"""Inspect a preview or replay versioned Hard Surface Workflow panel operators."""

import argparse
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import time
import tomllib
import uuid
from zipfile import ZipFile

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
PROJECT = next((parent for parent in ENTRY.parents if (parent / "blenderctl.ps1").is_file()), None)
if PROJECT is None:
    raise RuntimeError("Could not locate the project root")
TOOLS = PROJECT / "tools/blenderctl"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from filesystem import FileGuard, PathLocks
import panel_mode_handoff as mode_handoff
import panel_execute
import panel_selection_direction as selection_handoff
import panel_selection_preview as preview_handoff
import panel_surface_sweep as sweep_handoff
import panel_tool_target as tool_target
from material_projection_handoff import error_payload, run_worker
from protocol import (
    CODES,
    DEFAULT_BLENDER,
    DEFAULT_JOBS,
    SCHEMA,
    Failure,
    atomic_json,
    digest,
    envelope,
    read_json,
    timeout_value,
)


COMMAND = "model.panel-controls"
INSPECTIONS = {"inspect-preview", mode_handoff.INSPECT, sweep_handoff.INSPECT}
SELECTION_HANDOFFS = {adapter.OPERATION: adapter for adapter in (selection_handoff, preview_handoff)}
MODE_OPERATIONS = mode_handoff.MUTATIONS | SELECTION_HANDOFFS.keys() | {panel_execute.OPERATION}
OPERATIONS = INSPECTIONS | MODE_OPERATIONS | tool_target.OPERATIONS | {sweep_handoff.COMMIT, "reset-params", "reload-profile", "clear-preview"}
DEFAULT_EDITION = "0.3.1"
MINIMUM_BLENDER = (5, 3, 0)
RELEASES = {
    "0.3.1": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.1.zip",
        "08ce912197159c569e47ea42661b16ead83c48fdb7ed2af8682ac50dff7785de",
    ),
    "0.3.2": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.2.zip",
        "b02f31a0267ec22965389c0fc57109b29b7a6182ecf44686d22fda020a8b131b",
    ),
    "0.3.3": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.3.zip",
        "20f6ef5d0d4909c9613c6342819fea16fa9addad586cc30e03223d758575433b",
    ),
    "0.3.4": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.4.zip",
        "b978fde85c238ab0f2305300f16a9ad1851c81f88a463e0b89a224c0442f92de",
    ),
    "0.3.5": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.5.zip",
        "88149e1f6666520d603058d9d1437d925fc163403f76268ce5088f221835b7b7",
    ),
    "0.3.6": (
        PROJECT / "runtime/hard-surface/hard_surface_workflow-0.3.6.zip",
        "0dcb06844a6e467fc4599025aa589b3455a98256e0e37b9dcca63ff13833e381",
    ),
    "0.3.7": (
        PROJECT / "runtime/blender53-production/1.0.0/extensions/hard-surface-0.3.7.zip",
        "8493e040cc0b804fe2788ff7094709b904637cd4b67caa1eded5299a0c338699",
    ),
}
RELEASE_PACKAGE = "hard_surface_workflow_addon"
CODE_SUFFIXES = (".py", ".json", ".toml")
EMPTY_PREVIEW = {
    "preview_json": "", "preview_source": "", "preview_object_name": "",
    "preview_object_uid": 0, "preview_mesh_name": "", "preview_mesh_uid": 0,
    "preview_domain": "", "preview_hash": "", "preview_count": 0,
    "preview_indices": "", "preview_order": "",
}
PREVIEW_CLEARED_MESSAGE = "预览已清除；请先重新预览并确认选区再执行。"
PREVIEW_EMPTY_MESSAGE = "当前没有可清除的预览。"
EDIT_SELECTION_ATTRIBUTES = {".select_vert", ".select_edge", ".select_poly"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


class PanelFailure(Failure):
    """Failure that retains concrete panel state for a worker receipt."""

    def __init__(self, code, message, details=None):
        super().__init__(code, message)
        self.details = details


def parser():
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    cli.add_argument("--timeout", type=float, default=120)
    subparsers = cli.add_subparsers(dest="action", required=True, parser_class=Parser)
    action = subparsers.add_parser("panel-controls", allow_abbrev=False,
                                  description=__doc__)
    action.add_argument("--params", type=Path, required=True,
                         help=f"Requires Blender 5.3.0. Select edition=0.3.7 for the current local package. "
                              f"Optional JSON edition: {', '.join(RELEASES)}. The legacy default is {DEFAULT_EDITION}. "
                              "Inspect-preview takes operation, file and expected_sha256 and returns applicable clear and execute requests and the saved tool settings. "
                              "Inspect-mode takes the same source fields and returns a request for enter-edit or exit-edit. "
                              "Inspect-sweep (edition 0.3.7) takes operation, file, expected_sha256 and object, "
                              "naming a managed sweep or its control, with optional station_positions_mm for one station. "
                              "Commit-sweep takes its complete commit_request and requires a new --output. "
                              "Returned requests carry the edition used for inspection. "
                              "For pick-path-direction from 0.3.2 onward, use expected.mode_sha256 from inspect-mode, "
                              "an explicit path selector with seeds and sampling, and optional ordered_indices. "
                              "Preview-selection uses expected.mode_sha256 and a current, indices or seeded path selector. Coordinate box requires 0.3.2 or later. "
                              "Execute-preview takes the complete execute_request from inspect-preview and uses the saved native tool parameters and controls. "
                              "Configure-tool takes tool_id and changes (parameters and/or execution controls) and retains other saved values. "
                              "Choose-tool takes the source file and SHA plus tool_id. Use-active-object takes the source file and SHA plus the saved active mesh name as object. "
                              "Reset, clear and reload use object and the operation-specific expected values. Reload also takes plane_object and cutter_object.")
    action.add_argument("--output", type=Path, help="New .blend working copy for mutations; omit for inspections")
    return cli


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def _name(value):
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 63
            and "\0" not in value and all(ord(char) >= 32 for char in value))


def _sha256_text(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _preview_sha256(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None


def ensure_release(edition, job, checkpoint):
    """Read the pinned ZIP once and unpack it only into this new job."""
    archive_path, expected_sha256 = RELEASES[edition]
    require(archive_path.is_file(), "NOT_FOUND", f"Hard Surface Workflow {edition} ZIP is missing")
    with FileGuard(archive_path) as locked:
        payload = b"".join(locked.chunks(checkpoint))
    zip_sha256 = hashlib.sha256(payload).hexdigest()
    require(zip_sha256 == expected_sha256, "CONFLICT", "Hard-surface release SHA-256 changed")
    root = job / "release"
    root.mkdir(exist_ok=False)
    package = root / RELEASE_PACKAGE
    package.mkdir()
    files = {}
    with ZipFile(BytesIO(payload)) as archive:
        metadata = tomllib.loads(archive.read("blender_manifest.toml").decode("utf-8"))
        require(metadata.get("id") == "hard_surface_workflow" and metadata.get("version") == edition,
                "CONFLICT", "Hard-surface release manifest has the wrong product or edition")
        catalog = json.loads(archive.read("catalog.json"))
        require(catalog.get("version") == edition, "CONFLICT",
                "Hard-surface tool catalog has the wrong edition")
        if edition in {"0.3.5", "0.3.6"}:
            require((metadata.get("blender_version_min"), metadata.get("blender_version_max")) == ("5.3.0", "5.3.1")
                    and catalog.get("supported_blender") == ["5.3.0"], "CONFLICT",
                    "Unexpected compatibility metadata in the historical Hard Surface Workflow edition")
        for info in archive.infolist():
            if info.is_dir():
                continue
            checkpoint()
            relative = PurePosixPath(info.filename.replace(chr(92), "/"))
            target = package / relative
            require(relative.parts and not relative.is_absolute() and ".." not in relative.parts
                    and all(":" not in part for part in relative.parts)
                    and target.resolve().is_relative_to(package), "CONFLICT",
                    f"Invalid package member: {info.filename!r}")
            data = archive.read(info)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as out:
                out.write(data)
            expected = hashlib.sha256(data).hexdigest()
            require(digest(target, checkpoint) == expected, "CONFLICT",
                    f"Unpacked release member changed: {relative}")
            files[relative.as_posix()] = expected
    manifest_file = root / "files.json"
    atomic_json(manifest_file, sorted(files.items()))
    return {
        "edition": edition,
        "root": str(root), "package": str(package), "file_manifest": str(manifest_file),
        "zip": str(archive_path), "zip_sha256": zip_sha256, "files_checked": len(files),
        "code_data_files": sum(name.endswith(CODE_SUFFIXES) for name in files),
        "per_file_match": True,
    }


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    try:
        import mathutils
        if isinstance(value, (mathutils.Vector, mathutils.Euler,
                              mathutils.Quaternion, mathutils.Color)):
            return [float(item) for item in value]
        if isinstance(value, mathutils.Matrix):
            return [[float(item) for item in row] for row in value]
    except (ImportError, TypeError, ValueError):
        pass
    for attribute in ("to_list", "to_dict"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return _json_value(method())
            except (TypeError, ValueError, RuntimeError):
                pass
    if type(value).__name__ == "bpy_prop_array":
        return [_json_value(item) for item in value]
    if hasattr(value, "name"):
        return value.name
    try:
        return [_json_value(item) for item in value]
    except (TypeError, ValueError):
        return str(value)


def _group_values(group, excluded=()):
    names = sorted(
        name for name in group.bl_rna.properties.keys()
        if name not in {"rna_type"} and name not in set(excluded)
    )
    return names, {name: _json_value(getattr(group, name)) for name in names}


def _persistence(group, names):
    owner = group.id_data
    owner_rna = owner.bl_rna.identifier
    return {
        "owner_rna": owner_rna,
        "owner_name": owner.name,
        "owner_is_scene": owner_rna == "Scene",
        "owner_is_window_manager": owner_rna == "WindowManager",
        "skip_save_fields": sorted(
            name for name in names
            if getattr(group.bl_rna.properties[name], "is_skip_save", False)
        ),
        "runtime_fields": sorted(
            name for name in names
            if getattr(group.bl_rna.properties[name], "is_runtime", False)
        ),
    }


def _mesh_snapshot(scene, mode_mesh=None, ignore_edit_selection=False):
    objects = {}
    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.type != "MESH":
            continue
        witness = mode_mesh is not None and mode_mesh[0] == obj.data
        mesh = mode_mesh[1] if witness else obj.data
        attributes = mode_handoff.mesh_attributes(mesh) if witness else mesh.attributes
        if witness and ignore_edit_selection:
            attributes = [item for item in attributes if item.name not in EDIT_SELECTION_ATTRIBUTES]
        shape_keys = {
            block.name: [[round(float(value), 9) for value in point.co]
                         for point in block.data]
            for block in mesh.shape_keys.key_blocks
        } if mesh.shape_keys else {}
        objects[obj.name] = {
            "data": obj.data.name,
            "vertices": [[round(float(value), 9) for value in vertex.co]
                         for vertex in mesh.vertices],
            "edges": [sorted(edge.vertices) for edge in mesh.edges],
            "faces": [list(face.vertices) for face in mesh.polygons],
            "uv": {
                layer.name: [[round(float(value), 9) for value in item.uv]
                             for item in layer.data]
                for layer in mesh.uv_layers
            },
            "weights": [
                [[group.group, round(float(group.weight), 9)]
                 for group in vertex.groups]
                for vertex in mesh.vertices
            ],
            "vertex_groups": [group.name for group in obj.vertex_groups],
            "materials": [slot.material.name if slot.material else None
                          for slot in obj.material_slots],
            "modifiers": [[modifier.name, modifier.type]
                          for modifier in obj.modifiers],
            "shape_keys": shape_keys,
            "attributes": sorted(
                [attribute.name, attribute.domain, attribute.data_type,
                 len(attribute.data)]
                for attribute in attributes
            ),
        }
        if witness:
            objects[obj.name]["attribute_values"] = {
                attr.name: _sha256_text([
                    {prop.identifier: _json_value(getattr(item, prop.identifier))
                     for prop in item.bl_rna.properties if prop.identifier != "rna_type"}
                    for item in attr.data
                ]) for attr in attributes
            }
            objects[obj.name]["custom_normals"] = mesh.has_custom_normals
    return objects


def _collection_names(scene):
    result = []
    stack = [scene.collection]
    while stack:
        collection = stack.pop()
        result.append(collection.name)
        stack.extend(collection.children)
    return sorted(result)


def _object_states(scene):
    states = {}
    for obj in sorted(scene.objects, key=lambda item: item.name):
        states[obj.name] = {
            "type": obj.type,
            "data": obj.data.name if obj.data else None,
            "matrix_world": [[round(float(value), 9) for value in row]
                             for row in obj.matrix_world],
            "matrix_parent_inverse": [
                [round(float(value), 9) for value in row]
                for row in obj.matrix_parent_inverse
            ],
            "parent": obj.parent.name if obj.parent else None,
            "collections": sorted(collection.name
                                  for collection in obj.users_collection),
            "modifiers": [[modifier.name, modifier.type]
                          for modifier in obj.modifiers],
            "constraints": [[constraint.name, constraint.type]
                            for constraint in obj.constraints],
            "custom_properties": {
                key: _json_value(obj[key])
                for key in sorted(obj.keys())
                if not key.startswith("_")
            },
        }
    context_objects = scene.view_layers[0].objects
    active = context_objects.active if scene.view_layers else None
    return {
        "scene": scene.name,
        "objects": states,
        "active": active.name if active else None,
        "selected": sorted(obj.name for obj in context_objects
                           if obj.select_get()),
        "collections": _collection_names(scene),
        "frame": [scene.frame_current, round(float(scene.frame_subframe), 9)],
        "camera": scene.camera.name if scene.camera else None,
        "world": scene.world.name if scene.world else None,
    }


def _panel_snapshot(scene, mode_mesh=None, ignore_edit_selection=False):
    import hard_surface_workflow_addon.panel.settings as panel_state

    settings = scene.hsw_settings
    profile = scene.hsw_profile_settings
    settings_names, settings_values = _group_values(settings, excluded={"params"})
    profile_names, profile_values = _group_values(profile)
    param_names = sorted(
        name for name in settings.params.bl_rna.properties.keys()
        if name not in {"rna_type"}
    )
    params = {name: _json_value(getattr(settings.params, name))
              for name in param_names}
    params_persistence = _persistence(settings.params, param_names)
    try:
        tool_params = _json_value(
            panel_state.collect_params(settings, settings.tool)
        )
        tool_params_error = None
    except Exception as exc:
        tool_params = None
        tool_params_error = f"{type(exc).__name__}: {exc}"
    mesh = _mesh_snapshot(scene, mode_mesh, ignore_edit_selection)
    objects = _object_states(scene)
    mesh_sha256 = _sha256_text(mesh)
    scene_state = {"mesh_sha256": mesh_sha256, "objects": objects}
    return {
        "settings": {
            "values": settings_values,
            "params": params,
            "tool_params": tool_params,
            "tool_params_error": tool_params_error,
            "persistence": _persistence(settings, settings_names),
        },
        "profile": profile_values,
        "params_persistence": params_persistence,
        "profile_persistence": _persistence(profile, profile_names),
        "object_inventory": sorted(
            (obj.name, obj.type, [modifier.name for modifier in obj.modifiers])
            for obj in scene.objects
        ),
        "mesh_sha256": mesh_sha256,
        "scene_state_sha256": _sha256_text(scene_state),
        "settings_sha256": _sha256_text(settings_values),
        "profile_sha256": _sha256_text(profile_values),
        "rna": {
            "settings_class": settings.bl_rna.identifier,
            "params_class": settings.params.bl_rna.identifier,
            "params_owner": settings.params.id_data.bl_rna.identifier,
            "profile_class": profile.bl_rna.identifier,
            "settings_owner": settings.id_data.bl_rna.identifier,
            "profile_owner": profile.id_data.bl_rna.identifier,
            "param_properties": param_names,
            "settings_properties": settings_names,
            "profile_properties": profile_names,
        },
    }


def _approx_equal(actual, expected):
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and abs(float(actual) - float(expected)) <= 1e-6)
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_approx_equal(item, wanted)
                        for item, wanted in zip(actual, expected, strict=True)))
    return actual == expected


def _changed(before, after):
    return sorted(
        name for name in set(before) | set(after)
        if not _approx_equal(after.get(name), before.get(name))
    )


def _validate_reset_expected(panel, expected):
    import hard_surface_workflow_addon.panel.settings as panel_state

    require(set(expected) <= {"tool", "params"}, "INVALID_REQUEST",
            "reset-params expected accepts only tool and params")
    require("tool" in expected and _name(expected["tool"]),
            "INVALID_REQUEST",
            "reset-params expected.tool must be a tool identifier")
    require("params" in expected
            and isinstance(expected["params"], dict) and expected["params"],
            "INVALID_REQUEST", "reset-params requires nonempty expected.params")
    tool = expected["tool"]
    require(panel_state.descriptor(tool) is not None, "INVALID_REQUEST",
            f"Unknown hard-surface tool: {tool!r}")
    bindings = {binding.name: binding
                for binding in panel_state.iter_bindings(tool)}
    unknown = sorted(set(expected["params"]) - set(bindings))
    require(not unknown, "INVALID_REQUEST",
            f"Unknown parameters for {tool}: {unknown}")
    return tool, bindings


def _reset_expected(panel_state, tool):
    expected = {}
    affected = set()
    for binding in panel_state.iter_bindings(tool):
        if binding.kind == "scalar":
            affected.add(binding.prop)
            expected[binding.prop] = binding.value_default
            if binding.null_prop:
                affected.add(binding.null_prop)
                expected[binding.null_prop] = binding.null_default
        elif binding.kind == "flags":
            selected = {str(value) for value in (binding.default or ())}
            for prop, value, _label, _description in binding.choices:
                affected.add(prop)
                expected[prop] = value in selected
        elif binding.kind == "fixed":
            for prop, value in binding.items:
                affected.add(prop)
                expected[prop] = value
        elif binding.kind == "json":
            affected.add(binding.prop)
            expected[binding.prop] = binding.json_default
    return expected, affected


def _assert_panel(operation, before, after, request):
    require(after["mesh_sha256"] == before["mesh_sha256"], "VALIDATION_FAILED",
            f"{operation} changed mesh geometry")
    require(after["scene_state_sha256"] == before["scene_state_sha256"],
            "VALIDATION_FAILED",
            f"{operation} changed unrelated scene/object state")
    require(after["object_inventory"] == before["object_inventory"],
            "VALIDATION_FAILED",
            f"{operation} changed the object/modifier inventory")
    require(after["settings"]["persistence"] == before["settings"]["persistence"],
            "VALIDATION_FAILED", "Panel persistence metadata changed")
    require(after["profile_persistence"] == before["profile_persistence"],
            "VALIDATION_FAILED", "Profile persistence metadata changed")
    require(after["params_persistence"] == before["params_persistence"],
            "VALIDATION_FAILED",
            "Tool-parameter persistence metadata changed")

    if operation in tool_target.OPERATIONS:
        return tool_target.verify(operation, before, after, request)

    if operation in SELECTION_HANDOFFS:
        old, new = before["settings"]["values"], after["settings"]["values"]
        changed = _changed(old, new)
        unrelated = set(changed) - SELECTION_HANDOFFS[operation].ALLOWED_PANEL_CHANGES
        require(not unrelated and before["settings"]["params"] == after["settings"]["params"]
                and before["profile"] == after["profile"] and before["rna"] == after["rna"],
                "VALIDATION_FAILED", f"Selection preview changed unrelated panel fields: {sorted(unrelated)}")
        return {"kind": operation, "changed_settings_fields": changed}

    if operation == "clear-preview":
        before_values = before["settings"]["values"]
        prior = _preview_sha256(before_values["preview_json"])
        expected = {**before_values, "status_kind": "INFO",
                    "status": PREVIEW_CLEARED_MESSAGE if prior else PREVIEW_EMPTY_MESSAGE}
        if prior:
            expected.update(EMPTY_PREVIEW, needs_preview=True)
        require(after["settings"] == {**before["settings"], "values": expected}
                and after["profile"] == before["profile"] and after["rna"] == before["rna"],
                "VALIDATION_FAILED", "clear_preview changed unrelated settings or failed to clear the native preview")
        return {
            "kind": "clear-preview",
            "outcome": "cleared" if prior else "empty",
            "previous_preview_sha256": prior,
            "needs_preview": after["settings"]["values"]["needs_preview"],
            "changed_settings_fields": _changed(before_values, expected),
        }

    if operation == "reset-params":
        import hard_surface_workflow_addon.panel.settings as panel_state

        require(after["profile"] == before["profile"], "VALIDATION_FAILED",
                "reset_params changed profile panel fields")
        require(set(after["settings"]["params"])
                == set(before["settings"]["params"]),
                "VALIDATION_FAILED",
                "reset_params changed the parameter property set")
        tool, _bindings = _validate_reset_expected(before, request["expected"])
        require(before["settings"]["values"].get("tool") == tool
                and after["settings"]["values"].get("tool") == tool,
                "VALIDATION_FAILED",
                "reset_params did not operate on the expected tool")
        actual = after["settings"]["tool_params"]
        require(isinstance(actual, dict), "VALIDATION_FAILED",
                "Reset tool parameters could not be read back")
        for name, value in request["expected"]["params"].items():
            require(name in actual and _approx_equal(actual[name], value), "VALIDATION_FAILED",
                    f"reset_params did not produce requested default {name}")
        expected, affected = _reset_expected(panel_state, tool)
        for prop, value in expected.items():
            require(_approx_equal(
                after["settings"]["params"].get(prop), value
            ), "VALIDATION_FAILED",
                    f"reset_params did not restore {prop}")
        allowed = affected | {"status", "status_kind", "last_result"}
        for name, value in before["settings"]["values"].items():
            if name not in allowed:
                require(_approx_equal(
                    after["settings"]["values"].get(name), value
                ), "VALIDATION_FAILED",
                        f"reset_params changed unrelated panel field {name}")
        for prop, value in before["settings"]["params"].items():
            if prop not in affected:
                require(_approx_equal(
                    after["settings"]["params"].get(prop), value
                ), "VALIDATION_FAILED",
                        f"reset_params changed unrelated parameter {prop}")
        require(after["settings"]["values"].get("status_kind") == "SUCCESS",
                "VALIDATION_FAILED",
                "reset_params did not publish SUCCESS status")
        require(bool(after["settings"]["values"].get("status")),
                "VALIDATION_FAILED",
                "reset_params did not publish a status message")
        require(after["settings"]["values"].get("last_result") == "",
                "VALIDATION_FAILED",
                "reset_params did not clear last_result")
        return {
            "kind": "reset-params",
            "expected_widget_defaults": expected,
            "affected_widgets": sorted(affected),
            "changed_widgets": _changed(before["settings"]["params"],
                                        after["settings"]["params"]),
            "changed_settings_fields": _changed(
                before["settings"]["values"], after["settings"]["values"]
            ),
            "preserved_settings_fields": sorted(
                set(before["settings"]["values"]) - allowed
            ),
            "preserved_profile_fields": sorted(before["profile"]),
        }

    require(after["settings"] == before["settings"], "VALIDATION_FAILED",
            "profile_reload changed the main tool panel or its parameters")
    profile_names = set(before["profile"])
    unknown = sorted(set(request["expected"]) - profile_names)
    require(not unknown, "INVALID_REQUEST",
            f"Unknown profile fields: {unknown}")
    related = {
        "target_object", "plane_object", "cutter_object",
        "plane_mode", "plane_name", "plane_size", "plane_offset", "plane_flip",
        "plane_face", "plane_edge", "plane_use_face",
        "cutter_name", "cutter_shape", "cutter_center", "cutter_depth",
        "cutter_segments", "radius", "loaded_cutter",
        "status", "status_kind",
    }
    for field, value in request["expected"].items():
        require(_approx_equal(after["profile"].get(field), value),
                "VALIDATION_FAILED",
                f"profile_reload did not load {field}")
    for field, value in before["profile"].items():
        if field not in related:
            require(_approx_equal(after["profile"].get(field), value),
                    "VALIDATION_FAILED",
                    f"profile_reload changed unrelated profile field {field}")
    require(after["profile"].get("target_object")
            == before["profile"].get("target_object"),
            "VALIDATION_FAILED",
            "profile_reload changed target_object")
    require(after["profile"].get("status_kind") == "SUCCESS",
            "VALIDATION_FAILED",
            "profile_reload did not publish SUCCESS status")
    require(str(after["profile"].get("status", "")).startswith("Reloaded "),
            "VALIDATION_FAILED",
            "profile_reload did not publish its success message")
    return {
        "kind": "reload-profile",
        "expected_profile_fields": sorted(request["expected"]),
        "changed_profile_fields": _changed(before["profile"], after["profile"]),
        "preserved_profile_fields": sorted(profile_names - related),
        "preserved_settings_sha256": before["settings_sha256"],
    }


def _assert_execution_panel(before, after):
    old, new = before["settings"]["values"], after["settings"]["values"]
    changed = _changed(old, new)
    allowed = EMPTY_PREVIEW.keys() | {"last_result", "object_context", "needs_preview", "status", "status_kind"}
    unrelated = set(changed) - allowed
    require(not unrelated and before["settings"]["params"] == after["settings"]["params"]
            and before["settings"]["tool_params"] == after["settings"]["tool_params"]
            and before["settings"]["tool_params_error"] == after["settings"]["tool_params_error"]
            and before["settings"]["persistence"] == after["settings"]["persistence"]
            and before["profile"] == after["profile"] and before["rna"] == after["rna"]
            and before["profile_persistence"] == after["profile_persistence"]
            and before["params_persistence"] == after["params_persistence"]
            and before["object_inventory"] == after["object_inventory"],
            "VALIDATION_FAILED", f"Native execution changed saved parameters or unrelated panel settings: {sorted(unrelated)}")
    return {"kind": panel_execute.OPERATION, "changed_settings_fields": changed}


def panel_request(args, job, checkpoint, request, edition):
    operation = request.get("operation")
    require(isinstance(operation, str) and operation in OPERATIONS,
            "INVALID_REQUEST", "Unknown panel-controls operation")
    inspecting = operation in INSPECTIONS
    require((args.output is None) == inspecting, "INVALID_REQUEST",
            "Only inspect-preview, inspect-mode and inspect-sweep omit --output")
    common = {"operation", "file", "expected_sha256"}
    target_operation = operation in tool_target.OPERATIONS
    sweep_operation = operation in sweep_handoff.OPERATIONS
    if sweep_operation:
        require(edition == sweep_handoff.EDITION, "UNSUPPORTED",
                "Surface sweep panel operations require edition=0.3.7")
        common.add("object")
        if not inspecting:
            common.add("expected")
    elif target_operation:
        common |= tool_target.request_fields(operation)
    elif not inspecting:
        common |= {"object", "expected"}
    allowed = common | {"edition"} | ({"plane_object", "cutter_object"}
                        if operation == "reload-profile" else set())
    selection_adapter = SELECTION_HANDOFFS.get(operation)
    if operation == selection_handoff.OPERATION:
        require(edition in preview_handoff.PATH_PREVIEW_EDITIONS, "UNSUPPORTED",
                "The native path-direction operator requires a supported edition from 0.3.2 onward")
    if selection_adapter:
        allowed |= selection_adapter.FIELDS
    if sweep_operation:
        allowed.add(sweep_handoff.POSITIONS)
    require(set(request) <= allowed, "INVALID_REQUEST",
            f"Unexpected panel request fields: {sorted(set(request) - allowed)}")
    require(common <= set(request), "INVALID_REQUEST",
            "Missing panel request fields")
    require(isinstance(request["file"], str) and request["file"],
            "INVALID_REQUEST", "Expected a saved .blend path")
    expected_sha = request["expected_sha256"]
    require(isinstance(expected_sha, str) and len(expected_sha) == 64
            and expected_sha == expected_sha.lower()
            and all(char in "0123456789abcdef" for char in expected_sha),
            "INVALID_REQUEST", "Expected a lowercase SHA-256")
    if sweep_operation:
        sweep_handoff.validate(request, _name)
    elif target_operation:
        tool_target.validate(request)
    elif not inspecting:
        require(_name(request["object"]), "INVALID_REQUEST", "Expected a target object name")
        require(isinstance(request["expected"], dict) and request["expected"], "INVALID_REQUEST",
                "Expected a nonempty expected panel object")
    if operation == "reset-params":
        require(set(request["expected"]) <= {"tool", "params"}
                and "tool" in request["expected"]
                and _name(request["expected"]["tool"])
                and isinstance(request["expected"].get("params"), dict)
                and request["expected"]["params"],
                "INVALID_REQUEST",
                "reset-params requires expected.tool and expected.params")
    elif operation == "clear-preview":
        expected = request["expected"]
        preview_sha = expected.get("preview_sha256")
        require(set(expected) == {"preview_sha256"}
                and (preview_sha is None or isinstance(preview_sha, str) and len(preview_sha) == 64
                     and all(char in "0123456789abcdef" for char in preview_sha)),
                "INVALID_REQUEST", "clear-preview requires expected.preview_sha256 as lowercase SHA-256 or explicit null")
    elif operation == panel_execute.OPERATION:
        panel_execute.validate_expected(request["expected"])
    elif operation in MODE_OPERATIONS:
        mode_handoff.validate_expected(request["expected"])
        if selection_adapter:
            request = {**request, **selection_adapter.normalize_fields({**request, "edition": edition})}
    elif operation == "reload-profile":
        require({"plane_object", "cutter_object"} <= set(request),
                "INVALID_REQUEST",
                "Profile reload requires plane_object and cutter_object")
        for field in ("plane_object", "cutter_object"):
            require(request[field] is None or _name(request[field]),
                    "INVALID_REQUEST",
                    f"{field} must be a saved object name or null")
        require(all(isinstance(name, str) and name
                    for name in request["expected"]),
                "INVALID_REQUEST",
                "Profile reload expected keys must be names")
    source = Path(request["file"]).resolve()
    require(source.is_file() and source.suffix.lower() == ".blend",
            "NOT_FOUND", "Panel source .blend does not exist")
    with FileGuard(source) as source_guard:
        require(source_guard.sha256(checkpoint) == expected_sha,
                "CONFLICT",
                "Panel source SHA-256 does not match")
    release = ensure_release(edition, job, checkpoint)
    return {**request, "edition": edition, "file": str(source),
            "release_root": release["root"], "release": release}


def _target(scene, name):
    target = scene.objects.get(name)
    require(target is not None and target.type == "MESH", "NOT_FOUND",
            f"Panel target mesh is missing: {name!r}")
    require(not getattr(target, "library", None)
            and not getattr(target.data, "library", None),
            "UNSUPPORTED", "Panel target must be local and unlinked")
    return target


def _current_status(operation, panel):
    if operation != "reload-profile":
        return panel["settings"]["values"].get("status", "")
    return panel["profile"].get("status", "")


def _operator(scene, operation):
    import bpy

    if operation == "reset-params":
        operator = bpy.ops.hard_surface.reset_params
        operator_id = "hard_surface.reset_params"
    elif operation == "clear-preview":
        operator = bpy.ops.hard_surface.clear_preview
        operator_id = "hard_surface.clear_preview"
    elif operation in mode_handoff.MUTATIONS:
        operator_name = operation.replace("-", "_")
        operator = getattr(bpy.ops.hard_surface, operator_name)
        operator_id = f"hard_surface.{operator_name}"
    else:
        operator = bpy.ops.hard_surface.profile_reload
        operator_id = "hard_surface.profile_reload"
    try:
        poll = bool(operator.poll())
    except Exception as exc:
        raise PanelFailure(
            "WORKER_FAILED",
            f"{operator_id}.poll() raised {type(exc).__name__}: {exc}",
            {"operator_id": operator_id, "operator_poll": None,
             "operator_exception": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not poll:
        raise PanelFailure(
            "WORKER_FAILED", f"{operator_id}.poll() returned False",
            {"operator_id": operator_id, "operator_poll": False},
        )
    try:
        result = operator()
    except Exception as exc:
        # bpy.ops raises RuntimeError for an ERROR report even when the
        # published execute() body returned CANCELLED. Preserve that concrete
        # rejection instead of turning it into an opaque worker exception.
        return {
            "id": operator_id,
            "poll": poll,
            "result": ["CANCELLED"],
            "blender_report_type": "CANCELLED_RUNTIME_ERROR",
            "exception": f"{type(exc).__name__}: {exc}",
        }
    raw_result = sorted(result)
    return {
        "id": operator_id,
        "poll": poll,
        "result": raw_result,
        "blender_report_type": ("FINISHED" if raw_result == ["FINISHED"]
                                else "CANCELLED"),
    }


def _prepare_reload(scene, target, request):
    profile = scene.hsw_profile_settings
    require(profile.target_object == target, "CONFLICT",
            "Profile target_object must already name the requested target")
    setup = {}
    for field, key in (("plane_object", "plane_object"),
                       ("cutter_object", "cutter_object")):
        wanted = request[key]
        current = getattr(profile, field)
        current_name = current.name if current else None
        if wanted is None:
            if current is not None:
                setattr(profile, field, None)
                setup[field] = {"from": current_name, "to": None,
                                "source": "explicit-null-request"}
            continue
        require(current is not None and current.name == wanted, "CONFLICT",
                f"Source {field} is {current_name!r}, request asks {wanted!r}; "
                "selecting it would run the panel load callback before the "
                "profile_reload operator")
    return setup


def _mode_panel_snapshot(context, ignore_edit_selection=False):
    with mode_handoff.readable_mesh(context) as witness:
        return _panel_snapshot(context.scene, witness, ignore_edit_selection)


def _preview_target(context, edition):
    pinned = context.scene.hsw_settings.target_object
    return pinned or (context.active_object if edition in preview_handoff.PATH_PREVIEW_EDITIONS else None)


def _inspect_preview(context, request, source_sha, runtime):
    scene = context.scene
    settings = scene.hsw_settings
    prior = _preview_sha256(settings.preview_json)
    target = _preview_target(context, request["edition"])
    identity = None if target is None else {"object": target.name, "mesh": target.data.name if target.type == "MESH" else None}
    expected = {"preview_sha256": prior}
    clear_request = None
    if (target is not None and target.type == "MESH" and scene.objects.get(target.name) == target
            and not target.library and not target.data.library
            and (prior is None or (settings.preview_object_name, settings.preview_mesh_name) == (target.name, target.data.name))):
        clear_request = {"operation": "clear-preview", "edition": request["edition"],
                         "file": request["file"], "expected_sha256": source_sha,
                         "object": target.name, "expected": expected}
    execute_request, execute_blocked, execution_settings = None, None, None
    try:
        ready = panel_execute.check(context, request["edition"])
        execution_settings = {key: ready[key] for key in ("tool", "parameters", "controls")}
        execute_request = {"operation": panel_execute.OPERATION, "edition": request["edition"],
                           "file": request["file"], "expected_sha256": source_sha, "object": ready["object"],
                           "expected": {"tool": ready["tool"], "preview_sha256": ready["preview_sha256"]}}
        if identity is None:
            identity = {"object": ready["object"], "mesh": ready["mesh"]}
    except Failure as exc:
        execute_blocked = {"code": exc.code, "message": str(exc)}
        try:
            execution_settings = panel_execute.execution_settings(context)
        except Failure:
            pass
    after = digest(request["file"])
    require(source_sha == request["expected_sha256"] == after, "CONFLICT", "Panel source changed during inspection")
    return {
        "operation": "inspect-preview",
        "edition": request["edition"],
        "source": {"file": request["file"], "sha256_before": source_sha, "sha256_after": after, "unchanged": True},
        "release": request["release"],
        "scene": scene.name,
        "target": identity,
        "preview": {"present": prior is not None, "sha256": prior, "object": settings.preview_object_name or None,
                    "mesh": settings.preview_mesh_name or None, "domain": settings.preview_domain or None,
                    "topology_sha256": settings.preview_hash or None, "count": settings.preview_count,
                    "indices": settings.preview_indices, "order": settings.preview_order,
                    "needs_preview": settings.needs_preview, **panel_execute.preview_details(settings)},
        "expected": expected,
        "clear_request": clear_request,
        "execute_request": execute_request,
        "execute_blocked": execute_blocked,
        "execution_settings": execution_settings,
        "runtime": runtime,
    }


def work(phase, request_file):
    import bpy

    require(bpy.app.background and not bpy.app.online_access, "UNSUPPORTED",
            "Use the isolated offline Blender worker")
    host_version = tuple(bpy.app.version)
    require(host_version >= MINIMUM_BLENDER, "UNSUPPORTED",
            "Hard Surface Workflow requires Blender 5.3.0 or later")
    document = read_json(request_file)
    request = document["params"]
    edition = request["edition"]
    archive_path, archive_sha256 = RELEASES[edition]
    release = request["release"]
    require(release["edition"] == edition and release["zip"] == str(archive_path)
            and release["zip_sha256"] == archive_sha256,
            "CONFLICT", "Worker release identity differs from the selected edition")
    with (Path(release["package"]) / "blender_manifest.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    minimum = tuple(map(int, metadata["blender_version_min"].split(".")))
    maximum = metadata.get("blender_version_max")
    require(host_version >= minimum and (not maximum or host_version < tuple(map(int, maximum.split(".")))),
            "UNSUPPORTED", f"Selected Blender is outside the host range declared by edition {edition}")
    sys.path.insert(0, request["release_root"])
    import hard_surface_workflow_addon as addon
    require(Path(addon.__file__).resolve().parent == Path(release["package"]).resolve()
            and addon.bl_info["version"] == tuple(map(int, edition.split("."))),
            "CONFLICT", "Loaded hard-surface add-on differs from the selected edition")
    addon.register()
    source = request["stage"] if phase == "verify" else request["file"]
    source_sha_before = digest(source)
    require(bpy.ops.wm.open_mainfile(filepath=source, load_ui=True,
                                     use_scripts=False) == {"FINISHED"},
            "VALIDATION_FAILED", f"Could not open {source}")
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]),
            "UNSUPPORTED", "Panel source must match the Blender major/minor")
    scene = bpy.context.scene
    require(scene is not None, "NOT_FOUND", "Saved file has no active scene")
    runtime = {
        "blender": bpy.app.version_string,
        "build": bpy.app.build_hash.decode(),
        "addon_edition": edition,
        "pid": os.getpid(),
        "isolated_user_resources": os.environ.get("BLENDER_USER_RESOURCES"),
    }

    operation = request["operation"]
    if operation == "inspect-preview":
        return _inspect_preview(bpy.context, request, source_sha_before, runtime)
    if operation == sweep_handoff.INSPECT:
        before = _panel_snapshot(scene)
        result = sweep_handoff.inspect(bpy.context, request)
        require(before == _panel_snapshot(scene), "VALIDATION_FAILED",
                "Surface sweep inspection changed the saved panel or scene")
        source_sha_after = digest(source)
        require(source_sha_after == source_sha_before == request["expected_sha256"],
                "CONFLICT", "Panel source changed during sweep inspection")
        return {"operation": operation, "edition": edition, "release": release, "runtime": runtime,
                "source": {"file": source, "sha256_before": source_sha_before,
                           "sha256_after": source_sha_after, "unchanged": True}, **result}
    if operation == mode_handoff.INSPECT:
        state = mode_handoff.observe(bpy.context)
        result = mode_handoff.inspect(bpy.context, request, state)
        for follow_up in result["requests"].values():
            follow_up["edition"] = edition
        source_sha_after = digest(source)
        require(source_sha_after == source_sha_before == request["expected_sha256"],
                "CONFLICT", "Panel source changed during inspection")
        return {"operation": operation, "edition": edition, "release": release, "runtime": runtime,
                "source": {"file": source, "sha256_before": source_sha_before,
                           "sha256_after": source_sha_after, "unchanged": True}, **result}
    selection_adapter = SELECTION_HANDOFFS.get(operation)
    selection_operation = selection_adapter is not None
    execution_operation = operation == panel_execute.OPERATION
    target_operation = operation in tool_target.OPERATIONS
    sweep_operation = operation == sweep_handoff.COMMIT
    mode_operation = operation in MODE_OPERATIONS
    panel = _mode_panel_snapshot(bpy.context, selection_operation or execution_operation) if mode_operation else _panel_snapshot(scene)
    mode_state = mode_handoff.observe(bpy.context) if mode_operation else None
    protected = mode_handoff.scene_protection(bpy.context, _object_states(scene)) if mode_operation else None

    if phase == "verify":
        candidate_sha = digest(source)
        require(candidate_sha == source_sha_before, "CONFLICT",
                "Candidate changed while it was reopened")
        verified = {
            "panel": panel,
            "mesh_sha256": panel["mesh_sha256"],
            "scene_state_sha256": panel["scene_state_sha256"],
            "candidate_sha256": candidate_sha,
            "source_sha256_before": source_sha_before,
            "source_sha256_after": candidate_sha,
            "runtime": runtime,
        }
        if mode_operation:
            verified.update(mode=mode_state, mode_protected_sha256=_sha256_text(protected))
        if selection_operation:
            verified[selection_adapter.RESULT_FIELD] = selection_adapter.readback(bpy.context, request)
        if execution_operation:
            verified["execution_after"] = panel_execute.observe(bpy.context, request["object"])
        if sweep_operation:
            verified["sweep_after"] = sweep_handoff.observe(bpy.context, request["object"], committed=True)
        return verified

    target = None if target_operation or sweep_operation else _target(scene, request["object"])
    source_panel = panel
    if sweep_operation:
        sweep_before = sweep_handoff.observe(bpy.context, request["object"])
        sweep_protected = sweep_handoff.protection(_mesh_snapshot(scene), _object_states(scene), sweep_before)
        before, setup = source_panel, {}
    elif execution_operation:
        confirmed = panel_execute.check(bpy.context, edition, request["object"], request["expected"])
        execution_before = panel_execute.observe(bpy.context, request["object"])
        before, setup = source_panel, {}
    elif selection_operation:
        require(_sha256_text(mode_state) == request["expected"]["mode_sha256"], "CONFLICT",
                "The saved mode, target or selection differs from the inspected context")
        before, setup = source_panel, {}
    elif target_operation:
        before, setup = source_panel, {}
    elif mode_operation:
        mode_handoff.check_input(bpy.context, request, mode_state)
        setup = {}
        before = source_panel
    elif operation in {"reset-params", "clear-preview"}:
        settings = scene.hsw_settings
        selected_target = _preview_target(bpy.context, edition) if operation == "clear-preview" else settings.target_object
        require(selected_target == target, "CONFLICT", "Tool panel context does not name the requested target")
        if operation == "reset-params":
            tool, _bindings = _validate_reset_expected(source_panel, request["expected"])
            require(settings.tool == tool, "CONFLICT",
                    f"Source tool is {settings.tool!r}, request expects {tool!r}")
        else:
            saved = source_panel["settings"]["values"]
            actual = _preview_sha256(saved["preview_json"])
            require(actual == request["expected"]["preview_sha256"], "CONFLICT",
                    "Saved preview does not match the requested preview identity")
            if actual:
                require(saved["preview_object_name"] == target.name and saved["preview_mesh_name"] == target.data.name,
                        "CONFLICT", "Saved preview must belong to the named target object and mesh")
        setup = {}
        before = source_panel
    else:
        setup = _prepare_reload(scene, target, request)
        before = _panel_snapshot(scene) if setup else source_panel

    selection_evidence = None
    try:
        if sweep_operation:
            sweep_execution = sweep_handoff.commit(bpy.context, request)
            bpy.context.view_layer.update()
            operator = {"id": "surface_sweep.execute", "result": ["FINISHED"]}
        elif execution_operation:
            operator = panel_execute.execute()
        elif selection_operation:
            applied = selection_adapter.apply(bpy.context, request, mode_state)
            operator, setup, selection_evidence = applied["operator"], applied["setup"], applied["evidence"]
        elif target_operation:
            operator = tool_target.execute(bpy.context, request)
        else:
            operator = _operator(scene, operation)
    except PanelFailure as exc:
        exc.details = {
            **(exc.details or {}),
            "setup": setup,
            "panel_before": before,
            "source_panel": source_panel,
        }
        raise
    if operation == "exit-edit" and operator["result"] == ["FINISHED"]:
        mode_handoff.restore_empty_context(bpy.context, mode_state)
    after = _mode_panel_snapshot(bpy.context, selection_operation or execution_operation) if mode_operation else _panel_snapshot(scene)
    if operator["result"] != ["FINISHED"]:
        raise PanelFailure(
            "WORKER_FAILED",
            f"{operator['id']} returned {operator['result']}; "
            f"status={_current_status(operation, after)!r}",
            {"operator": operator, "setup": setup,
             "panel_before": before, "panel_after": after,
             "source_panel": source_panel},
        )
    mode_after = mode_handoff.observe(bpy.context) if mode_operation else None
    try:
        if sweep_operation:
            sweep_after = sweep_handoff.observe(bpy.context, request["object"], committed=True)
            require(sweep_after["signature"] == sweep_execution["signature"]
                    and sweep_after["output"] == sweep_execution["output"],
                    "VALIDATION_FAILED", "Surface sweep did not persist the reported signed output")
            require(sweep_protected == sweep_handoff.protection(_mesh_snapshot(scene), _object_states(scene), sweep_after)
                    and all(before[key] == after[key] for key in ("settings", "profile", "params_persistence", "profile_persistence")),
                    "VALIDATION_FAILED", "Surface sweep changed unrelated panel or scene state")
            evidence = {"kind": sweep_handoff.COMMIT, "protected_scene_unchanged": True,
                        "status": sweep_execution["status"]}
        elif execution_operation:
            require(protected == mode_handoff.scene_protection(bpy.context, _object_states(scene)),
                    "VALIDATION_FAILED", "Native execution changed unrelated scene or view-layer state")
            execution_after = panel_execute.observe(bpy.context, request["object"])
            execution = panel_execute.verify_effect(confirmed, execution_before, execution_after, mode_state, mode_after)
            evidence = _assert_execution_panel(before, after)
        elif selection_operation:
            protected_after = mode_handoff.scene_protection(bpy.context, _object_states(scene))
            fields = set(mode_state) - {"mesh_select_mode", "element_selection"}
            require(protected == protected_after and all(mode_after[key] == mode_state[key] for key in fields),
                    "VALIDATION_FAILED", "Selection preview changed unrelated editing context")
            evidence = _assert_panel(operation, before, after, request)
        elif mode_operation:
            protected_after = mode_handoff.scene_protection(bpy.context, _object_states(scene))
            evidence = mode_handoff.verify_transition(operation, mode_state, mode_after, before, after, protected, protected_after)
        else:
            evidence = _assert_panel(operation, before, after, request)
    except Failure as exc:
        raise PanelFailure(exc.code, str(exc), {
            "operator": operator,
            "setup": setup,
            "panel_before": before,
            "panel_after": after,
            "source_panel": source_panel,
        }) from exc

    bpy.context.preferences.filepaths.save_version = 0
    stage = Path(request["stage"])
    require(not stage.exists(), "CONFLICT",
            "Private panel stage already exists")
    require(bpy.ops.wm.save_as_mainfile(
        filepath=str(stage), copy=True, relative_remap=True,
        check_existing=False
    ) == {"FINISHED"}, "IO_ERROR", "Could not save the panel candidate")
    require(stage.is_file(), "IO_ERROR",
            "Panel candidate was not written")
    saved = _mode_panel_snapshot(bpy.context, selection_operation or execution_operation) if mode_operation else _panel_snapshot(scene)
    require(saved == after, "VALIDATION_FAILED",
            "Saving the candidate changed panel or scene state")
    if mode_operation:
        require(mode_after == mode_handoff.observe(bpy.context), "VALIDATION_FAILED",
                "Saving the candidate changed the native mode or selection")
    source_sha_after = digest(source)
    require(source_sha_after == source_sha_before, "CONFLICT",
            "Panel source changed during authoring")
    authored = {
        "operation": operation,
        "edition": edition,
        "release": request["release"],
        "source": {"file": request["file"],
                   "sha256_before": source_sha_before,
                   "sha256_after": source_sha_after},
        "source_sha256": source_sha_after,
        "candidate_sha256": digest(stage),
        "operator": operator,
        "input_setup": setup,
        "source_panel_sha256": _sha256_text(source_panel),
        "panel_before": before,
        "panel_after": saved,
        "panel_evidence": evidence,
        "mesh_sha256_before": before["mesh_sha256"],
        "mesh_sha256_after": saved["mesh_sha256"],
        "scene_state_sha256_before": before["scene_state_sha256"],
        "scene_state_sha256_after": saved["scene_state_sha256"],
        "object_inventory": saved["object_inventory"],
        "runtime": runtime,
    }
    if mode_operation:
        authored.update(mode_before=mode_state, mode_after=mode_after, mode_protected_sha256=_sha256_text(protected))
    if selection_operation:
        authored[selection_adapter.RESULT_FIELD] = selection_evidence
    if execution_operation:
        authored.update(execution=execution, execution_after=execution_after)
    if sweep_operation:
        authored.update(sweep_execution=sweep_execution, sweep_before=sweep_before, sweep_after=sweep_after)
    return authored


def worker(phase, request_file):
    try:
        result = {"ok": True, "data": work(phase, request_file), "error": None}
    except Exception as exc:
        result = {"ok": False, "data": None,
                  "error": error_payload(exc, "VALIDATION_FAILED")}
        details = getattr(exc, "details", None)
        if details is not None:
            result["details"] = details
    atomic_json(Path(request_file).parent / (phase + "-result.json"), result)


def _private_stage_remove(stage, output):
    if stage is None or output is None:
        return
    try:
        stage = Path(stage).resolve()
        output = Path(output).resolve()
        if (stage.parent == output.parent
                and stage.name.startswith(".hswpanel-")
                and stage.suffix.lower() == ".blend"):
            stage.unlink(missing_ok=True)
    except OSError:
        pass


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    blender = Path(args.blender).resolve()
    require(blender.is_file(), "NOT_FOUND",
            f"Blender executable not found: {blender}")
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {
        "job_directory": str(job),
        "request": str(job / "request.json"),
        "result": str(job / "result.json"),
    }
    data = error = None
    output = stage = edition = None

    def checkpoint():
        require(not (job / "cancel.request").exists(), "CANCELLED",
                "Panel operation cancelled before publication")
        require(time.monotonic() < deadline, "TIMEOUT",
                "Panel operation exceeded its timeout")

    def status(state, phase):
        atomic_json(job / "status.json", {
            "state": state,
            "job_id": job.name,
            "pid": os.getpid(),
            "phase": phase,
            "heartbeat_at": time.time(),
        })

    def finish():
        for log in sorted(job.glob("*.log")):
            artifacts[log.name] = str(log)
        for name in ("request.json", "status.json", "inspect-result.json", "author-result.json", "verify-result.json"):
            path = job / name
            if path.is_file():
                artifacts[name] = str(path)
        result = envelope(COMMAND, job.name, data, error, artifacts)
        atomic_json(job / "result.json", result)
        terminal = {"TIMEOUT": "timed_out", "CANCELLED": "cancelled"}
        status(terminal.get(error["code"], "failed") if error else "succeeded", "done")
        return result

    status("running", "verify_inputs")
    try:
        with ExitStack() as stack:
            request_file = args.params.resolve()
            require(request_file.is_file(), "NOT_FOUND", "Panel request JSON is missing")
            with FileGuard(request_file):
                raw = read_json(request_file)
                require(isinstance(raw, dict), "INVALID_REQUEST", "Panel request must be a JSON object")
                selected = raw.get("edition", DEFAULT_EDITION)
                require(isinstance(selected, str), "INVALID_REQUEST", "Panel edition must be a version string")
                require(selected in RELEASES, "UNSUPPORTED",
                        f"Unsupported Hard Surface Workflow edition {selected!r}; choose {', '.join(RELEASES)}")
                edition = selected
                request = panel_request(args, job, checkpoint, raw, edition)
            if request["operation"] in INSPECTIONS:
                source_guard = stack.enter_context(FileGuard(request["file"]))
                require(source_guard.sha256(checkpoint) == request["expected_sha256"],
                        "CONFLICT", "Panel source changed before inspection")
                atomic_json(job / "request.json", {"schema_version": SCHEMA, "command": COMMAND, "params": request})
                status("running", "inspect")
                observed = run_worker(blender, job, "inspect", deadline, checkpoint, status, entry=ENTRY, private_temp=True)
                require(observed["source"]["sha256_after"] == source_guard.sha256(checkpoint) == request["expected_sha256"],
                        "VALIDATION_FAILED", "Panel source changed during inspection")
                data = observed
                artifacts["release_manifest"] = request["release"]["file_manifest"]
                return finish()
            require(args.output.is_absolute(), "INVALID_REQUEST", "Expected an absolute .blend output")
            output = args.output.resolve()
            require(output.is_absolute() and output.suffix.lower() == ".blend",
                    "INVALID_REQUEST",
                    "Expected a new absolute .blend output")
            require(output != Path(request["file"]).resolve(),
                    "INVALID_REQUEST",
                    "Output must not replace the source")
            require(not os.path.lexists(output), "CONFLICT",
                    f"Output already exists: {output}")
            output.parent.mkdir(parents=True, exist_ok=True)
            for directory in output.parents:
                if directory != Path(directory.anchor):
                    stack.enter_context(FileGuard(directory, directory=True))
            stack.enter_context(PathLocks([output]))
            source_guard = stack.enter_context(FileGuard(Path(request["file"])))
            require(source_guard.sha256(checkpoint) == request["expected_sha256"],
                    "CONFLICT", "Panel source changed before authoring")
            new_stage = output.parent / (".hswpanel-" + job.name + ".blend")
            require(not os.path.lexists(new_stage), "CONFLICT",
                    "Private panel stage already exists")
            stage = new_stage
            request["stage"] = str(stage)
            atomic_json(job / "request.json", {
                "schema_version": SCHEMA,
                "command": COMMAND,
                "params": request,
            })
            status("running", "author")
            authored = run_worker(
                blender, job, "author", deadline, checkpoint, status,
                entry=ENTRY, private_temp=True,
            )
            status("running", "verify")
            verified = run_worker(
                blender, job, "verify", deadline, checkpoint, status,
                entry=ENTRY, private_temp=True,
            )
            require(authored["candidate_sha256"]
                    == verified["candidate_sha256"],
                    "VALIDATION_FAILED",
                    "Candidate changed during independent reopen")
            require(authored["panel_after"] == verified["panel"],
                    "VALIDATION_FAILED",
                    "Panel state did not survive independent reopen")
            require(authored["mesh_sha256_after"] == verified["mesh_sha256"],
                    "VALIDATION_FAILED",
                    "Geometry changed during independent reopen")
            require(authored["scene_state_sha256_after"]
                    == verified["scene_state_sha256"],
                    "VALIDATION_FAILED",
                    "Scene/object state changed during independent reopen")
            require(authored["runtime"]["pid"] != verified["runtime"]["pid"],
                    "VALIDATION_FAILED",
                    "Independent reopen reused the authoring process")
            require(authored["runtime"]["addon_edition"] == verified["runtime"]["addon_edition"] == edition,
                    "VALIDATION_FAILED", "Independent reopen used a different add-on edition")
            if request["operation"] in MODE_OPERATIONS:
                require(authored["mode_after"] == verified["mode"]
                        and authored["mode_protected_sha256"] == verified["mode_protected_sha256"],
                        "VALIDATION_FAILED", "Native mode, selection or protected data did not survive independent reopening")
            selection_adapter = SELECTION_HANDOFFS.get(request["operation"])
            if selection_adapter:
                field = selection_adapter.RESULT_FIELD
                require(authored[field] == verified[field],
                        "VALIDATION_FAILED", "Native selection preview did not survive independent reopening")
            execution_operation = request["operation"] == panel_execute.OPERATION
            if execution_operation:
                require(authored["execution_after"] == verified["execution_after"], "VALIDATION_FAILED",
                        "The native mesh effect, object identities or protected data did not survive independent reopening")
            sweep_operation = request["operation"] == sweep_handoff.COMMIT
            if sweep_operation:
                require(authored["sweep_after"] == verified["sweep_after"], "VALIDATION_FAILED",
                        "Surface sweep signature, mesh or station controls did not survive independent reopening")
            require(authored["source"]["sha256_after"]
                    == request["expected_sha256"]
                    == source_guard.sha256(checkpoint),
                    "VALIDATION_FAILED",
                    "Source SHA changed during authoring")
            require(verified["source_sha256_before"]
                    == verified["source_sha256_after"]
                    == verified["candidate_sha256"],
                    "CONFLICT",
                    "Candidate changed while being verified")
            with FileGuard(stage, renameable=True) as locked:
                require(locked.sha256(checkpoint)
                        == verified["candidate_sha256"],
                        "CONFLICT",
                        "Private stage changed after verification")
                checkpoint()
                locked.rename_new(output)

            data = {
                "operation": request["operation"],
                "edition": edition,
                "release": request["release"],
                "source": {
                    "file": request["file"],
                    "sha256_before": request["expected_sha256"],
                    "sha256_after": authored["source"]["sha256_after"],
                    "unchanged": (
                        request["expected_sha256"]
                        == authored["source"]["sha256_after"]
                    ),
                },
                "candidate": {
                    "file": str(output),
                    "sha256": verified["candidate_sha256"],
                },
                "operator": authored["operator"],
                "input_setup": authored["input_setup"],
                "source_panel_sha256": authored["source_panel_sha256"],
                "panel_before": authored["panel_before"],
                "panel_after": authored["panel_after"],
                "panel_evidence": authored["panel_evidence"],
                "authoring_runtime": authored["runtime"],
                "layout_evidence": {
                    "mesh_sha256_before": authored["mesh_sha256_before"],
                    "mesh_sha256_after": authored["mesh_sha256_after"],
                    "scene_state_sha256_before":
                        authored["scene_state_sha256_before"],
                    "scene_state_sha256_after":
                        authored["scene_state_sha256_after"],
                    "object_inventory": authored["object_inventory"],
                    "source_sha256_before": request["expected_sha256"],
                    "source_sha256_after": authored["source"]["sha256_after"],
                },
                "independent_reopen": {
                    "process": "fresh_process",
                    "panel": verified["panel"],
                    "candidate_sha256": verified["candidate_sha256"],
                    "mesh_sha256": verified["mesh_sha256"],
                    "scene_state_sha256": verified["scene_state_sha256"],
                    **verified["runtime"],
                },
            }
            if request["operation"] in MODE_OPERATIONS:
                data.update(mode_before=authored["mode_before"], mode_after=authored["mode_after"])
                data["independent_reopen"]["mode"] = verified["mode"]
            if execution_operation:
                data["execution"] = authored["execution"]
                data["independent_reopen"]["execution"] = verified["execution_after"]
                source_fields = {"file": str(output), "expected_sha256": verified["candidate_sha256"], "edition": edition}
                data["preview_inspect_request"] = {"operation": "inspect-preview", **source_fields}
                data["mode_inspect_request"] = {"operation": mode_handoff.INSPECT, **source_fields}
            if sweep_operation:
                data.update(sweep_execution=authored["sweep_execution"], sweep_before=authored["sweep_before"])
                data["independent_reopen"]["sweep"] = verified["sweep_after"]
                data["sweep_inspect_request"] = {"operation": sweep_handoff.INSPECT, "edition": edition,
                                                 "file": str(output), "expected_sha256": verified["candidate_sha256"],
                                                 "object": request["object"]}
            if selection_adapter:
                data[selection_adapter.RESULT_FIELD] = verified[selection_adapter.RESULT_FIELD]
                source_fields = {"file": str(output), "expected_sha256": verified["candidate_sha256"], "edition": edition}
                data["preview_inspect_request"] = {"operation": "inspect-preview", **source_fields}
                data["mode_inspect_request"] = {"operation": mode_handoff.INSPECT, **source_fields}
            artifacts.update(
                candidate=str(output),
                author_result=str(job / "author-result.json"),
                independent_reopen=str(job / "verify-result.json"),
                release_manifest=request["release"]["file_manifest"],
            )
    except (Failure, OSError, RuntimeError, ValueError, TypeError,
            KeyError, ImportError) as exc:
        data, error = None, error_payload(exc, "IO_ERROR")
        if edition is not None:
            error["message"] = f"Hard Surface Workflow {edition}: {error['message']}"
        _private_stage_remove(stage, output)
    return finish()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        result = envelope(COMMAND, error=error_payload(exc, "INVALID_REQUEST"))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(result["error"]["code"], 1)


if __name__ == "__main__":
    if "--background" in sys.argv and "--" in sys.argv:
        phase, request_path = sys.argv[sys.argv.index("--") + 1:]
        worker(phase, Path(request_path))
    else:
        raise SystemExit(main())
