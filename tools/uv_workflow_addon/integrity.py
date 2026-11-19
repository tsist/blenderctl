"""Deformation-state protection for editable UV MESH targets.

The module is intentionally independent of the command-line inspection package.
It has two public entry points:

``local_supported(objects, writable=True)``
    Assert that every target can be represented by the certified field set.
    With ``writable=True`` shared Mesh/Key targets are refused; with
    ``writable=False`` a read-only shared source is allowed.  Raises
    :class:`IntegrityError` with a stable ``code`` for unsupported RNA.

``snapshot(objects, writable=False)``
    Return a JSON-safe, deterministic protection snapshot.  Large arrays are
    hashed in bulk and only digests, counts and non-finite counts are emitted.
    It includes target-related armature dependencies and target constraints.

``dependencies(objects, writable=False)``
    Return only the target-referenced rig dependency state (armatures and
    target links), suitable for a parent scene_fingerprint adapter or a cold
    read comparison.

Scope and non-goals
-------------------
Base positions, base topology, existing UV coordinates, pins, seams and the
current world matrix are deliberately *not* duplicated here; ``core.protection``
already covers them.  This module adds the fields that core protection does not
cover: shape-key identity/settings/all key coordinates, active/show-only/
edit-mode key state, vertex-group definitions and weights, target constraints,
representable modifier configuration/references, target object/Mesh/Key
animation and driver definitions, effective material-slot links, face material
indices, and non-UV mesh attributes.  It also protects rest/pose/constraints and
real animation keyframes of armatures directly referenced by target armature
modifiers or parent chains, without scanning unrelated scene objects.

The module never evaluates a dependency graph, never changes the current frame,
never enables auto-execution, and never writes a datablock.  It does not certify
the final evaluated surface.  Non-target/global rig/action state, material node
content, linked library content, constraint-target rig content, node-group graph
content, and arbitrary RNA remain the parent's full-scene/protection and
final-surface checks.  Generic modifiers are represented by their writable
configuration and references only; unknown Geometry Nodes graphs are refused,
not claimed as fully covered.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from array import array

import bpy


SCHEMA = "uv_workflow_deformation_integrity_v1"

# Modifier types whose complete writable scalar/vector/enum/string properties and
# direct ID references are represented by the bounded generic writer below.
# The list is deliberately a certified subset, not "all modifiers".
CERTIFIED_MODIFIERS = frozenset({
    "ARRAY",
    "ARMATURE",
    "BEVEL",
    "CAST",
    "CORRECTIVE_SMOOTH",
    "CURVE",
    "DISPLACE",
    "EDGE_SPLIT",
    "HOOK",
    "LAPLACIANDEFORM",
    "LAPLACIANSMOOTH",
    "LATTICE",
    "MESH_DEFORM",
    "MIRROR",
    "MASK",
    "NORMAL_EDIT",
    "BOOLEAN",
    "SHRINKWRAP",
    "SIMPLE_DEFORM",
    "SMOOTH",
    "SOLIDIFY",
    "SUBSURF",
    "SURFACE_DEFORM",
    "TRIANGULATE",
    "VOLUME_DISPLACE",
    "WARP",
    "WAVE",
    "WEIGHTED_NORMAL",
})

# Native Smooth by Angle graphs admitted by surface._shading_tree.  This is a
# capability set, not a node-group name match.  NODES modifiers with any other
# node type remain refused; their content is not certified here.
SHADING_NODES = frozenset({
    "NodeGroupInput",
    "NodeGroupOutput",
    "NodeReroute",
    "NodeFrame",
    "GeometryNodeGroup",
    "FunctionNodeBooleanMath",
    "FunctionNodeCompare",
    "GeometryNodeInputEdgeSmooth",
    "GeometryNodeInputMeshEdgeAngle",
    "GeometryNodeInputShadeSmooth",
    "GeometryNodeSetShadeSmooth",
})

# UI-only modifier fields do not describe deformation.  Excluding them prevents
# a panel being opened from invalidating a candidate, while real show/render
# switches remain explicit fields.
_MODIFIER_UI_FIELDS = frozenset({
    "rna_type",
    "id_data",
    "show_expanded",
    "show_options",
    "show_preview",
    "show_texture",
    "is_active",
    "debug_options",
})

_MODIFIER_COMMON_FIELDS = frozenset({
    "name",
    "type",
    "show_viewport",
    "show_render",
    "show_in_editmode",
    "show_on_cage",
})

# These attributes either duplicate core protection or are edit-selection UI.
# Do not skip "." generically: .sculpt_mask and .sculpt_face_set are meaningful
# editable entity data.  Only known selection/hide/topology/pin temporaries are
# excluded.
_ATTRIBUTE_SKIP = frozenset({
    "position",
    ".position",
    "material_index",
    ".material_index",
    "uv_seam",
    ".uv_seam",
    "uv_pin",
    ".uv_pin",
    ".select_vert",
    ".select_edge",
    ".select_poly",
    ".select_face",
    ".corner_vert",
    ".corner_edge",
    ".edge_verts",
    ".hide_vert",
    ".hide_edge",
    ".hide_poly",
    ".hide_face",
})

_RNA_SCALAR_TYPES = frozenset({"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"})
_RNA_UI_PREFIXES = ("open_", "show_")
_MAX_COLLECTION = 512
_MAX_RECURSION = 8
_MAX_CUSTOM_ITEMS = 4096

_BONE_REST_SETTINGS = (
    "use_connect",
    "use_deform",
    "head_radius",
    "tail_radius",
    "envelope_distance",
    "envelope_weight",
    "use_envelope_multiply",
    "inherit_scale",
    "use_inherit_rotation",
    "use_local_location",
    "use_relative_parent",
    "bbone_segments",
    "bbone_x",
    "bbone_z",
    "bbone_easein",
    "bbone_easeout",
    "bbone_curveinx",
    "bbone_curveinz",
    "bbone_curveoutx",
    "bbone_curveoutz",
    "bbone_rollin",
    "bbone_rollout",
    "bbone_scalein",
    "bbone_scaleout",
    "bbone_handle_type_start",
    "bbone_handle_type_end",
    "bbone_custom_handle_start",
    "bbone_custom_handle_end",
    "bbone_handle_use_ease_start",
    "bbone_handle_use_ease_end",
    "bbone_handle_use_scale_start",
    "bbone_handle_use_scale_end",
    "bbone_mapping_mode",
    "use_endroll_as_inroll",
    "use_scale_easing",
)

_POSE_BONE_SETTINGS = (
    "ik_stretch",
    "ik_stiffness_x",
    "ik_stiffness_y",
    "ik_stiffness_z",
    "use_ik_limit_x",
    "use_ik_limit_y",
    "use_ik_limit_z",
    "ik_min_x",
    "ik_max_x",
    "ik_min_y",
    "ik_max_y",
    "ik_min_z",
    "ik_max_z",
    "lock_ik_x",
    "lock_ik_y",
    "lock_ik_z",
    "use_ik_rotation_control",
    "use_ik_linear_control",
    "ik_linear_weight",
    "ik_rotation_weight",
    "bbone_curveinx",
    "bbone_curveinz",
    "bbone_curveoutx",
    "bbone_curveoutz",
    "bbone_rollin",
    "bbone_rollout",
    "bbone_scalein",
    "bbone_scaleout",
    "bbone_easein",
    "bbone_easeout",
    "bbone_segments",
    "bbone_x",
    "bbone_z",
    "bbone_custom_handle_start",
    "bbone_custom_handle_end",
)

_CONSTRAINT_UI_FIELDS = frozenset({
    "rna_type",
    "id_data",
    "name",
    "type",
    "enabled",
    "influence",
    "mute",
    "show_expanded",
    "show_options",
    "show_texture",
    "show_preview",
    "is_valid",
    "active",
})


def _skip_attribute(name, uv_names):
    if name in _ATTRIBUTE_SKIP:
        return True
    # Only the real pin attribute for an existing UV layer is a core-owned
    # temporary; a custom ".pn.semantic" attribute must remain protected.
    return name in {".pn." + uv_name for uv_name in uv_names}


class IntegrityError(RuntimeError):
    """A stable, machine-readable refusal or validation failure."""

    def __init__(self, code, message):
        self.code = str(code)
        self.message = str(message)
        super().__init__("[%s] %s" % (self.code, self.message))


def _fail(code, message):
    raise IntegrityError(code, message)


def _unsupported(message):
    _fail("UNSUPPORTED", message)


def _invalid(message):
    _fail("VALIDATION_FAILED", message)


def _conflict(message):
    _fail("CONFLICT", message)


def _is_id(value):
    return isinstance(value, bpy.types.ID)


def _library_identity(library):
    if library is None:
        return None
    # Use the stored path, not a resolved absolute path.  This is stable across
    # a cold reopen in the same project and does not depend on cwd.
    return {"name": library.name, "filepath": library.filepath}


def _identity(item):
    if item is None:
        return None
    if not _is_id(item):
        _unsupported("Expected a Blender ID datablock, got %r" % (type(item).__name__,))
    return {
        "type": item.bl_rna.identifier,
        "name": item.name,
        "library": _library_identity(item.library),
        "override_library": bool(getattr(item, "override_library", False)),
    }


def _json_number(value):
    value = float(value)
    if not math.isfinite(value):
        return {"non_finite": repr(value)}
    return value


def _text_bytes(value):
    return str(value).encode("utf-8")


def _hash_update_text(digest, value):
    raw = _text_bytes(value)
    digest.update(struct.pack("<I", len(raw)))
    digest.update(raw)


def _hash_update_array(digest, values, tag, kind, width):
    _hash_update_text(digest, tag)
    _hash_update_text(digest, kind)
    digest.update(struct.pack("<II", int(width), int(len(values))))
    digest.update(values.tobytes())


def _array_digest(values, tag, kind="f", width=1):
    digest = hashlib.sha256()
    _hash_update_array(digest, values, tag, kind, width)
    return digest.hexdigest()


def _count_nonfinite(values):
    """Count non-finite float32 values without a Python loop when possible."""
    try:
        import numpy as np
        return int(np.count_nonzero(~np.isfinite(np.frombuffer(values, dtype=np.float32))))
    except Exception:
        return sum(1 for value in values if not math.isfinite(value))


def _idprops_summary(item):
    """Hash arbitrary editable ID properties with explicit bounds."""
    try:
        keys = sorted(item.keys())
    except TypeError:
        keys = []
    visible = [key for key in keys if key != "_RNA_UI"]
    if len(visible) > _MAX_CUSTOM_ITEMS:
        _unsupported("Too many custom properties on %s" % item.name)
    digest = hashlib.sha256()
    digest.update(struct.pack("<I", len(visible)))
    for key in visible:
        _hash_update_text(digest, key)
        _hash_idprop(digest, item[key], 0)
    return {
        "count": len(visible),
        "keys": visible[:_MAX_COLLECTION],
        "keys_truncated": len(visible) > _MAX_COLLECTION,
        "sha256": digest.hexdigest(),
    }


def _hash_idprop(digest, value, depth):
    if depth > _MAX_RECURSION:
        _unsupported("Custom-property nesting exceeds the certified depth")
    if value is None:
        digest.update(b"N")
        return
    if isinstance(value, bool):
        digest.update(b"B" + bytes((1 if value else 0,)))
        return
    if isinstance(value, int):
        digest.update(b"I" + struct.pack("<q", int(value)))
        return
    if isinstance(value, float):
        digest.update(b"F" + struct.pack("<d", float(value)))
        return
    if isinstance(value, str):
        digest.update(b"S")
        _hash_update_text(digest, value)
        return
    if _is_id(value):
        digest.update(b"D")
        _hash_update_text(digest, json.dumps(_identity(value), sort_keys=True,
                                             separators=(",", ":"), ensure_ascii=False))
        return
    if hasattr(value, "keys") and not isinstance(value, (str, bytes, bytearray)):
        keys = sorted(value.keys())
        if len(keys) > _MAX_CUSTOM_ITEMS:
            _unsupported("Custom-property group is too large")
        digest.update(b"G" + struct.pack("<I", len(keys)))
        for key in keys:
            _hash_update_text(digest, key)
            _hash_idprop(digest, value[key], depth + 1)
        return
    if hasattr(value, "to_list"):
        values = value.to_list()
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        _unsupported("Uncertified custom-property value type %s" % type(value).__name__)
    if len(values) > _MAX_CUSTOM_ITEMS:
        _unsupported("Custom-property array is too large")
    digest.update(b"A" + struct.pack("<I", len(values)))
    for item in values:
        _hash_idprop(digest, item, depth + 1)


def _rna_scalar(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _json_number(value)
    if _is_id(value):
        return _identity(value)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_COLLECTION:
            _unsupported("RNA array is too large")
        return [_rna_scalar(item) for item in value]
    if hasattr(value, "to_list"):
        values = value.to_list()
        if len(values) > _MAX_COLLECTION:
            _unsupported("RNA array is too large")
        return [_rna_scalar(item) for item in values]
    # mathutils Vector/Matrix/Color/Euler are iterable and finite.
    try:
        values = list(value)
    except TypeError:
        _unsupported("Uncertified RNA scalar type %s" % type(value).__name__)
    if len(values) > _MAX_COLLECTION:
        _unsupported("RNA array is too large")
    return [_rna_scalar(item) for item in values]


def _rna_configuration(item, prefix="", depth=2, seen=None, exclude=()):
    """Bounded writable-RNA writer for certified modifiers and FCurve modifiers.

    Returns ``(settings, references)``.  ID pointers are kept as stable
    identities in ``references``; nested structs are recursively represented
    with a hard depth and collection bound.  Unknown RNA kinds are refused
    rather than treated as certified.
    """
    if seen is None:
        seen = set()
    pointer = getattr(item, "as_pointer", lambda: 0)()
    if pointer and pointer in seen:
        _unsupported("RNA cycle in %s" % item.bl_rna.identifier)
    if pointer:
        seen = set(seen)
        seen.add(pointer)
    settings = {}
    references = {}
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "id_data"} or name in exclude:
            continue
        if name.startswith(_RNA_UI_PREFIXES):
            continue
        if prop.is_readonly:
            continue
        try:
            raw = getattr(item, name)
        except Exception as exc:
            _unsupported("Cannot read %s.%s: %s" % (item.bl_rna.identifier, name, exc))
        if prop.type in _RNA_SCALAR_TYPES:
            settings[name] = _rna_scalar(raw)
            continue
        if prop.type == "POINTER":
            if raw is None:
                references[name] = None
            elif _is_id(raw):
                references[name] = _identity(raw)
            elif isinstance(raw, bpy.types.bpy_struct):
                if depth <= 0:
                    _unsupported("Nested RNA depth exceeded at %s.%s" % (item.bl_rna.identifier, name))
                sub_settings, sub_references = _rna_configuration(
                    raw, prefix=prefix + name + ".", depth=depth - 1, seen=seen, exclude=exclude)
                settings[name] = {
                    "type": raw.bl_rna.identifier,
                    "settings": sub_settings,
                }
                for key, value in sub_references.items():
                    references[name + "." + key] = value
            else:
                _unsupported("Uncertified RNA pointer %s.%s" % (item.bl_rna.identifier, name))
            continue
        if prop.type == "COLLECTION":
            try:
                count = len(raw)
            except Exception as exc:
                _unsupported("Cannot count %s.%s: %s" % (item.bl_rna.identifier, name, exc))
            if count > _MAX_COLLECTION:
                _unsupported("RNA collection %s.%s exceeds the certified size" %
                             (item.bl_rna.identifier, name))
            rows = []
            for index, entry in enumerate(raw):
                if _is_id(entry):
                    rows.append(_identity(entry))
                elif isinstance(entry, bpy.types.bpy_struct):
                    if depth <= 0:
                        _unsupported("Nested RNA collection depth exceeded at %s.%s" %
                                     (item.bl_rna.identifier, name))
                    sub_settings, sub_references = _rna_configuration(
                        entry, prefix=prefix + name + "[%d]." % index,
                        depth=depth - 1, seen=seen, exclude=exclude)
                    rows.append({
                        "type": entry.bl_rna.identifier,
                        "settings": sub_settings,
                        "references": sub_references,
                    })
                else:
                    rows.append(_rna_scalar(entry))
            settings[name] = rows
            continue
        _unsupported("Uncertified RNA property kind %s.%s (%s)" %
                     (item.bl_rna.identifier, name, prop.type))
    return settings, references


def _shading_tree(tree, stack=()):
    """Admit the native Smooth by Angle graph by capabilities, not by name."""
    if tree is None or getattr(tree, "as_pointer", lambda: 0)() in stack:
        return False
    if getattr(tree, "bl_idname", None) != "GeometryNodeTree":
        return False
    pointer = tree.as_pointer()
    for node in tree.nodes:
        if node.bl_idname not in SHADING_NODES:
            return False
        if node.bl_idname == "GeometryNodeGroup":
            if not _shading_tree(node.node_tree, (*stack, pointer)):
                return False
    return True


def _is_shading_nodes_modifier(modifier):
    return (modifier.type == "NODES" and
            getattr(modifier, "node_group", None) is not None and
            _shading_tree(modifier.node_group))


def _nodes_inputs_state(modifier):
    """Explicitly read Geometry Nodes modifier interface inputs.

    modifier.properties is a read-only interface object; inputs are exposed as
    small read-only socket structs with writable scalar/string values.  We only
    traverse this known interface, not arbitrary read-only runtime properties.
    """
    try:
        interface = getattr(modifier, "properties", None)
        inputs = getattr(interface, "inputs", None) if interface is not None else None
        if inputs is None:
            return {"present": False, "sockets": []}
        sockets = []
        for prop in inputs.bl_rna.properties:
            name = prop.identifier
            if name in {"rna_type", "name"}:
                continue
            if len(sockets) >= _MAX_COLLECTION:
                _unsupported("Geometry Nodes modifier inputs exceed the certified size")
            socket = getattr(inputs, name)
            settings, references = _rna_configuration(
                socket,
                exclude={"rna_type", "id_data", "name", "type"},
                depth=1,
            )
            sockets.append({
                "property": name,
                "socket_type": getattr(socket, "type", None),
                "settings": settings,
                "references": references,
            })
        return {"present": True, "sockets": sockets}
    except IntegrityError:
        raise
    except Exception as exc:
        _unsupported("Cannot inspect Geometry Nodes modifier inputs: %s" % exc)


def _hook_vertex_indices_state(modifier):
    indices = getattr(modifier, "vertex_indices", None)
    if indices is None:
        _unsupported("HOOK modifier %s has no readable vertex_indices" % modifier.name)
    try:
        values = array("i", (int(value) for value in indices))
    except Exception as exc:
        _unsupported("Cannot read HOOK vertex_indices on %s: %s" %
                     (modifier.name, exc))
    return {
        "count": len(values),
        "sha256": _array_digest(values, "hook_vertex_indices", "i", 1),
    }


def _modifier_state(modifier):
    settings, references = _rna_configuration(
        modifier,
        exclude=_MODIFIER_UI_FIELDS | _MODIFIER_COMMON_FIELDS,
    )
    known = modifier.type in CERTIFIED_MODIFIERS
    state = {
        "name": modifier.name,
        "type": modifier.type,
        "show_viewport": bool(getattr(modifier, "show_viewport", True)),
        "show_render": bool(getattr(modifier, "show_render", True)),
        "show_in_editmode": bool(getattr(modifier, "show_in_editmode", False)),
        "show_on_cage": bool(getattr(modifier, "show_on_cage", False)),
        "settings": settings,
        "references": references,
        "custom_properties": _idprops_summary(modifier),
        "coverage": ("known_modifier_writable_rna_config_only_v2" if known
                     else "generic_writable_rna_config_only_v2"),
        "surface_evaluated": "not_certified",
        "referenced_content_owner": "parent_full_scene",
    }
    if modifier.type == "NODES":
        state["shading_tree"] = True
        state["coverage"] = "native_shading_nodes_writable_rna_config_only_v2"
        state["inputs"] = _nodes_inputs_state(modifier)
        state["node_group_content_coverage"] = (
            "identity_and_modifier_inputs_only; parent scene_fingerprint owns node-group content"
        )
        state["referenced_content_owner"] = "parent_scene_fingerprint_for_node_group_content"
    if modifier.type == "HOOK":
        state["vertex_indices"] = _hook_vertex_indices_state(modifier)
    return state


def _slot_identifier(slot):
    if slot is None:
        return None
    identifier = getattr(slot, "identifier", None)
    if not identifier:
        _unsupported("Action slot has no stable identifier")
    return str(identifier)


def _action_key(action):
    return json.dumps(_identity(action), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _envelope_control_points_state(modifier):
    points = getattr(modifier, "control_points", None)
    if points is None:
        _unsupported("FModifierEnvelope has no readable control_points collection")
    if len(points) > _MAX_COLLECTION:
        _unsupported("FModifierEnvelope control_points exceed the certified size")
    rows = []
    for point in points:
        settings, references = _rna_configuration(
            point,
            exclude={"rna_type", "id_data"},
            depth=1,
        )
        rows.append({
            "frame": _json_number(point.frame),
            "min": _json_number(point.min),
            "max": _json_number(point.max),
            "settings": settings,
            "references": references,
        })
    return {"count": len(rows), "control_points": rows}


def _fcurve_modifier_state(modifier):
    exclude = {"rna_type", "id_data", "show_expanded", "active"}
    if modifier.type == "ENVELOPE":
        exclude.add("control_points")
    settings, references = _rna_configuration(
        modifier,
        exclude=exclude,
        depth=1,
    )
    state = {
        "type": modifier.type,
        "is_valid": bool(getattr(modifier, "is_valid", True)),
        "settings": settings,
        "references": references,
    }
    if modifier.type == "ENVELOPE":
        state["control_points"] = _envelope_control_points_state(modifier)
    return state


def _fcurve_modifier_digest(fcurve):
    if len(fcurve.modifiers) > _MAX_COLLECTION:
        _unsupported("FCurve modifier count exceeds the certified size")
    modifiers = [_fcurve_modifier_state(item) for item in fcurve.modifiers]
    payload = json.dumps(modifiers, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return (len(modifiers), hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            modifiers)


def _keyframe_digest(fcurve):
    digest = hashlib.sha256()
    points = fcurve.keyframe_points
    digest.update(struct.pack("<I", len(points)))
    for point in points:
        digest.update(struct.pack("<dd", float(point.co[0]), float(point.co[1])))
        digest.update(struct.pack("<dd", float(point.handle_left[0]), float(point.handle_left[1])))
        digest.update(struct.pack("<dd", float(point.handle_right[0]), float(point.handle_right[1])))
        _hash_update_text(digest, point.interpolation)
        _hash_update_text(digest, point.easing)
        _hash_update_text(digest, point.handle_left_type)
        _hash_update_text(digest, point.handle_right_type)
        digest.update(struct.pack("<ddd", float(point.back), float(point.amplitude), float(point.period)))
    return digest.hexdigest()


def _sampled_points_state(fcurve):
    try:
        points = fcurve.sampled_points
        count = len(points)
    except Exception as exc:
        _unsupported("Cannot read FCurve sampled_points: %s" % exc)
    if not count:
        return {"count": 0, "non_finite": 0,
                "sha256": _array_digest(array("f"), "fcurve_sampled_points", "f", 2)}
    values = array("f", [0.0]) * (count * 2)
    try:
        points.foreach_get("co", values)
    except Exception:
        values = array("f")
        for point in points:
            try:
                values.extend((float(point.co[0]), float(point.co[1])))
            except Exception as exc:
                _unsupported("Cannot read FCurve sampled point coordinate: %s" % exc)
    return {
        "count": count,
        "non_finite": _count_nonfinite(values),
        "sha256": _array_digest(values, "fcurve_sampled_points", "f", 2),
    }


def _fcurve_state(fcurve):
    count, modifiers_digest, modifier_states = _fcurve_modifier_digest(fcurve)
    group = getattr(fcurve, "group", None)
    return {
        "data_path": fcurve.data_path,
        "array_index": int(fcurve.array_index),
        "mute": bool(fcurve.mute),
        "extrapolation": fcurve.extrapolation,
        "auto_smoothing": getattr(fcurve, "auto_smoothing", None),
        "is_valid": bool(getattr(fcurve, "is_valid", True)),
        "group": group.name if group else None,
        "keyframe_count": len(fcurve.keyframe_points),
        "keyframe_sha256": _keyframe_digest(fcurve),
        "sampled_points": _sampled_points_state(fcurve),
        "modifier_count": count,
        "modifier_sha256": modifiers_digest,
        "modifiers": modifier_states,
    }


def _driver_target_state(target):
    return {
        "id": _identity(getattr(target, "id", None)),
        "id_type": getattr(target, "id_type", None),
        "data_path": getattr(target, "data_path", ""),
        "bone_target": getattr(target, "bone_target", ""),
        "transform_type": getattr(target, "transform_type", None),
        "transform_space": getattr(target, "transform_space", None),
        "rotation_mode": getattr(target, "rotation_mode", None),
        "context_property": getattr(target, "context_property", None),
        "use_fallback_value": bool(getattr(target, "use_fallback_value", False)),
        "fallback_value": _json_number(getattr(target, "fallback_value", 0.0)),
    }


def _driver_state(fcurve):
    driver = fcurve.driver
    count, modifiers_digest, _modifier_states = _fcurve_modifier_digest(fcurve)
    variables = []
    for variable in driver.variables:
        variables.append({
            "name": variable.name,
            "type": variable.type,
            "targets": [_driver_target_state(target) for target in variable.targets],
        })
    return {
        "data_path": fcurve.data_path,
        "array_index": int(fcurve.array_index),
        "mute": bool(fcurve.mute),
        "type": driver.type,
        "expression": driver.expression,
        "is_simple_expression": bool(getattr(driver, "is_simple_expression", False)),
        "is_valid": bool(getattr(driver, "is_valid", False)),
        "use_self": bool(getattr(driver, "use_self", False)),
        "variables": variables,
        "fcurve": _fcurve_state(fcurve),
        "modifier_count": count,
        "modifier_sha256": modifiers_digest,
    }


def _action_strip_state(strip):
    channelbags = []
    for bag in strip.channelbags:
        identifier = _slot_identifier(bag.slot)
        channelbags.append({
            "slot": identifier,
            "fcurves": sorted(
                [_fcurve_state(fcurve) for fcurve in bag.fcurves],
                key=lambda row: (row["data_path"], row["array_index"]),
            ),
        })
    channelbags.sort(key=lambda row: row["slot"] or "")
    return {"type": strip.type, "channelbags": channelbags}


def _action_state(action):
    layers = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            if strip.type != "KEYFRAME" or not hasattr(strip, "channelbags"):
                _unsupported("Action layer strip type %s is not certified" % strip.type)
            strips.append(_action_strip_state(strip))
        layers.append({"name": layer.name, "strips": strips})
    legacy = []
    if not layers:
        try:
            legacy_curves = list(action.fcurves)
        except Exception:
            legacy_curves = []
        legacy = sorted(
            [_fcurve_state(fcurve) for fcurve in legacy_curves],
            key=lambda row: (row["data_path"], row["array_index"]),
        )
    slots = [
        {"identifier": _slot_identifier(slot), "target_id_type": slot.target_id_type}
        for slot in sorted(action.slots, key=lambda item: _slot_identifier(item) or "")
    ]
    return {
        "identity": _identity(action),
        "id_root": getattr(action, "id_root", None),
        "frame_range": [_json_number(value) for value in action.frame_range],
        "use_frame_range": bool(action.use_frame_range),
        "use_cyclic": bool(action.use_cyclic),
        "slots": slots,
        "layers": layers,
        "legacy_fcurves": legacy,
        "custom_properties": _idprops_summary(action),
    }


def _nla_strip_state(strip):
    action = getattr(strip, "action", None)
    try:
        curves = sorted(
            [_fcurve_state(fcurve) for fcurve in strip.fcurves],
            key=lambda row: (row["data_path"], row["array_index"]),
        )
    except IntegrityError:
        raise
    except Exception as exc:
        _unsupported("Cannot read NLA strip FCurves on %s: %s" % (strip.name, exc))
    try:
        modifiers = [_fcurve_modifier_state(item) for item in strip.modifiers]
    except IntegrityError:
        raise
    except Exception as exc:
        _unsupported("Cannot read NLA strip modifiers on %s: %s" % (strip.name, exc))
    return {
        "name": strip.name,
        "type": strip.type,
        "action": _identity(action),
        "action_key": _action_key(action) if action else None,
        "action_slot": _slot_identifier(getattr(strip, "action_slot", None)),
        "frame_start": _json_number(strip.frame_start),
        "frame_end": _json_number(strip.frame_end),
        "action_frame_start": _json_number(strip.action_frame_start),
        "action_frame_end": _json_number(strip.action_frame_end),
        "repeat": _json_number(strip.repeat),
        "scale": _json_number(strip.scale),
        "blend_type": strip.blend_type,
        "extrapolation": strip.extrapolation,
        "use_auto_blend": bool(strip.use_auto_blend),
        "blend_in": _json_number(strip.blend_in),
        "blend_out": _json_number(strip.blend_out),
        "influence": _json_number(strip.influence),
        "strip_time": _json_number(strip.strip_time),
        "use_animated_influence": bool(strip.use_animated_influence),
        "use_animated_time": bool(strip.use_animated_time),
        "use_animated_time_cyclic": bool(strip.use_animated_time_cyclic),
        "use_reverse": bool(strip.use_reverse),
        "use_sync_length": bool(strip.use_sync_length),
        "mute": bool(strip.mute),
        "fcurves": curves,
        "modifiers": modifiers,
    }


def _nla_track_state(track):
    return {
        "name": track.name,
        "mute": bool(track.mute),
        "is_solo": bool(track.is_solo),
        "strips": [_nla_strip_state(strip) for strip in track.strips],
    }


def _animation_state(animation_data):
    if animation_data is None:
        return None
    action = animation_data.action
    actions = {}
    if action is not None:
        actions[_action_key(action)] = _action_state(action)
    for track in animation_data.nla_tracks:
        for strip in track.strips:
            strip_action = getattr(strip, "action", None)
            if strip_action is not None:
                actions[_action_key(strip_action)] = _action_state(strip_action)
    return {
        "action": _identity(action),
        "action_key": _action_key(action) if action else None,
        "action_slot": _slot_identifier(getattr(animation_data, "action_slot", None)),
        "action_extrapolation": animation_data.action_extrapolation,
        "action_blend_type": animation_data.action_blend_type,
        "action_influence": _json_number(animation_data.action_influence),
        "use_nla": bool(animation_data.use_nla),
        "use_tweak_mode": bool(animation_data.use_tweak_mode),
        "actions": {key: actions[key] for key in sorted(actions)},
        "nla_tracks": [_nla_track_state(track) for track in animation_data.nla_tracks],
        "drivers": sorted(
            [_driver_state(fcurve) for fcurve in animation_data.drivers],
            key=lambda row: (row["data_path"], row["array_index"]),
        ),
    }


def _vector_state(values):
    return [_json_number(value) for value in values]


def _matrix_state(rows):
    return [[_json_number(value) for value in row] for row in rows]


def _armature_constraint_targets_state(constraint):
    targets = getattr(constraint, "targets", None)
    if targets is None:
        _unsupported("Armature constraint %s has no readable targets collection" %
                     constraint.name)
    if len(targets) > _MAX_COLLECTION:
        _unsupported("Armature constraint targets exceed the certified size")
    rows = []
    for target in targets:
        settings, references = _rna_configuration(
            target,
            exclude={"rna_type", "id_data"},
            depth=1,
        )
        rows.append({"settings": settings, "references": references})
    return {"count": len(rows), "targets": rows}


def _constraint_state(constraint):
    exclude = set(_CONSTRAINT_UI_FIELDS)
    if constraint.type == "ARMATURE":
        exclude.add("targets")
    settings, references = _rna_configuration(
        constraint,
        exclude=exclude,
        depth=2,
    )
    state = {
        "name": constraint.name,
        "type": constraint.type,
        "enabled": bool(constraint.enabled),
        "influence": _json_number(constraint.influence),
        "mute": bool(getattr(constraint, "mute", False)),
        "settings": settings,
        "references": references,
        "custom_properties": _idprops_summary(constraint),
        "coverage": "bounded_writable_scalar_vector_idref_v1",
    }
    if constraint.type == "ARMATURE":
        state["targets"] = _armature_constraint_targets_state(constraint)
    return state


_SPECIAL_BONE_EXACT = frozenset({
    "head_radius",
    "tail_radius",
    "use_endroll_as_inroll",
    "use_envelope_multiply",
    "use_scale_easing",
})


def _special_value_nondefault(prop, value):
    if value is None:
        return False
    if isinstance(value, (bool, int, float, str)):
        return value != getattr(prop, "default", None)
    try:
        items = list(value)
    except Exception:
        return True
    return any(bool(item) for item in items)


def _check_special_bone_coverage(owner, known, label):
    known = set(known)
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if not (name.startswith("bbone_") or name.startswith("envelope_") or
                name in _SPECIAL_BONE_EXACT):
            continue
        if name in known:
            continue
        try:
            value = getattr(owner, name)
        except Exception as exc:
            _unsupported("Cannot inspect B-Bone/envelope field %s on %s: %s" %
                         (name, label, exc))
        if _special_value_nondefault(prop, value):
            _unsupported("Non-default unrepresented B-Bone/envelope field %s on %s" %
                         (name, label))


def _bone_rna_setting(bone, name):
    value = getattr(bone, name)
    if name not in {"bbone_custom_handle_start", "bbone_custom_handle_end"} or value is None:
        return _rna_scalar(value)
    owner = bone.id_data
    if not isinstance(value, type(bone)) or value.id_data != owner:
        _unsupported("Uncertified B-Bone handle owner or type on %s.%s" % (bone.name, name))
    try:
        path = value.path_from_id()
        resolved = owner.path_resolve(path)
    except (TypeError, ValueError) as exc:
        _unsupported("Cannot resolve B-Bone handle on %s.%s: %s" % (bone.name, name, exc))
    if resolved != value:
        _unsupported("B-Bone handle path mismatch on %s.%s" % (bone.name, name))
    return {"type": value.bl_rna.identifier, "owner": _identity(owner), "data_path": path}


def _bone_rest_state(bone):
    _check_special_bone_coverage(bone, _BONE_REST_SETTINGS, "bone " + bone.name)
    row = {
        "name": bone.name,
        "parent": bone.parent.name if bone.parent else None,
        "head_local": _vector_state(bone.head_local),
        "tail_local": _vector_state(bone.tail_local),
        "matrix_local": _matrix_state(bone.matrix_local),
        "custom_properties": _idprops_summary(bone),
    }
    for name in _BONE_REST_SETTINGS:
        if hasattr(bone, name):
            row[name] = _bone_rna_setting(bone, name)
    return row


def _pose_bone_state(pose_bone):
    _check_special_bone_coverage(pose_bone, _POSE_BONE_SETTINGS,
                                 "pose bone " + pose_bone.name)
    row = {
        "name": pose_bone.name,
        "parent": pose_bone.parent.name if pose_bone.parent else None,
        "rotation_mode": pose_bone.rotation_mode,
        "location": _vector_state(pose_bone.location),
        "rotation_euler": _vector_state(pose_bone.rotation_euler),
        "rotation_quaternion": _vector_state(pose_bone.rotation_quaternion),
        "rotation_axis_angle": _vector_state(pose_bone.rotation_axis_angle),
        "scale": _vector_state(pose_bone.scale),
        "matrix_basis": _matrix_state(pose_bone.matrix_basis),
        "matrix": _matrix_state(pose_bone.matrix),
        "constraints": [_constraint_state(c) for c in pose_bone.constraints],
        "custom_properties": _idprops_summary(pose_bone),
    }
    for name in _POSE_BONE_SETTINGS:
        if hasattr(pose_bone, name):
            row[name] = _bone_rna_setting(pose_bone, name)
    return row


def _related_rigs(objects):
    """Collect only rigs referenced by targets, never a full-scene object scan."""
    rigs = {}
    target_links = {}
    for obj in objects:
        modifier_rows = []
        for modifier in obj.modifiers:
            if modifier.type != "ARMATURE":
                continue
            rig = getattr(modifier, "object", None)
            modifier_rows.append({
                "modifier": modifier.name,
                "object": _identity(rig),
            })
            if rig is not None:
                if rig.type != "ARMATURE":
                    _unsupported("Armature modifier %s on %s references a non-armature object" %
                                 (modifier.name, obj.name))
                rigs[rig.name] = rig
        parent_chain = []
        parent = obj.parent
        depth = 0
        while parent is not None and depth < 16:
            parent_chain.append(_identity(parent))
            if parent.type == "ARMATURE":
                rigs[parent.name] = parent
            parent = parent.parent
            depth += 1
        if parent is not None:
            _unsupported("Parent chain on %s exceeds the certified depth" % obj.name)
        target_links[obj.name] = {
            "armature_modifiers": modifier_rows,
            "parent": _identity(obj.parent),
            "parent_type": obj.parent_type,
            "parent_bone": obj.parent_bone,
            "parent_chain": parent_chain,
        }
    return rigs, target_links


def _rig_state(rig):
    data = rig.data
    return {
        "identity": _identity(rig),
        "data": _identity(data),
        "use_fake_user": bool(rig.use_fake_user),
        "parent": _identity(rig.parent),
        "parent_type": rig.parent_type,
        "parent_bone": rig.parent_bone,
        "matrix_world": _matrix_state(rig.matrix_world),
        "matrix_basis": _matrix_state(rig.matrix_basis),
        "matrix_parent_inverse": _matrix_state(rig.matrix_parent_inverse),
        "constraints": [_constraint_state(c) for c in rig.constraints],
        "animation": _animation_state(rig.animation_data),
        "custom_properties": _idprops_summary(rig),
        "data_state": {
            "identity": _identity(data),
            "users": int(data.users),
            "use_fake_user": bool(data.use_fake_user),
            "pose_position": data.pose_position,
            "animation": _animation_state(data.animation_data),
            "custom_properties": _idprops_summary(data),
            "bones": [_bone_rest_state(bone) for bone in data.bones],
        },
        "pose": {
            "bones": [_pose_bone_state(bone) for bone in rig.pose.bones],
        },
    }


def _check_rig(rig):
    if rig.type != "ARMATURE":
        _unsupported("Related rig %s is not an ARMATURE object" % rig.name)
    if rig.mode not in {"OBJECT", "POSE"}:
        _conflict("Related rig %s is in %s mode; object/pose mode required" %
                  (rig.name, rig.mode))
    data = rig.data
    if data is None:
        _invalid("Related rig %s has no armature data" % rig.name)
    # Driver safety precedes all pose/rest/constraint value reads.
    _check_animation_data(rig.animation_data, "rig %s" % rig.name,
                          allow_linked_actions=True)
    _check_animation_data(data.animation_data, "rig data %s" % data.name,
                          allow_linked_actions=True)
    for constraint in rig.constraints:
        _constraint_state(constraint)
    bone_names = {bone.name for bone in data.bones}
    pose_names = {bone.name for bone in rig.pose.bones}
    if bone_names != pose_names:
        _invalid("Rig %s rest/pose bone sets differ" % rig.name)
    for bone in data.bones:
        _bone_rest_state(bone)
    for bone in rig.pose.bones:
        _pose_bone_state(bone)
    _idprops_summary(rig)
    _idprops_summary(data)
    return None


def _related_rigs_checked(objects):
    rigs, target_links = _related_rigs(objects)
    for name in sorted(rigs):
        _check_rig(rigs[name])
    return rigs, target_links


def _dependencies_state(objects):
    rigs, target_links = _related_rigs(objects)
    return {
        "armatures": {
            name: _rig_state(rigs[name]) for name in sorted(rigs)
        },
        "target_links": {
            name: target_links[name] for name in sorted(target_links)
        },
    }


def dependencies(objects, writable=False):
    """Return only target-referenced rig dependencies as a JSON-safe dict."""
    items = list(objects)
    local_supported(items, writable=writable)
    return {"schema": SCHEMA, **_dependencies_state(items)}


def _attribute_plan(attribute):
    plans = {
        "FLOAT": (("value", 1, "f"),),
        "INT": (("value", 1, "i"),),
        "BOOLEAN": (("value", 1, "b"),),
        "INT8": (("value", 1, "i"),),
        "INT16_2D": (("value", 2, "i"),),
        "INT32_2D": (("value", 2, "i"),),
        "FLOAT2": (("vector", 2, "f"),),
        "FLOAT_VECTOR": (("vector", 3, "f"),),
        "FLOAT4": (("vector", 4, "f"),),
        "QUATERNION": (("value", 4, "f"),),
        "FLOAT4X4": (("value", 16, "f"),),
        "FLOAT_COLOR": (("color", 4, "f"), ("color_srgb", 4, "f")),
        "BYTE_COLOR": (("color", 4, "f"), ("color_srgb", 4, "f")),
        "STRING": (("value", 1, "s"),),
    }
    plan = plans.get(attribute.data_type)
    if plan is None:
        _unsupported("Uncertified mesh attribute data type %s (%s)" %
                     (attribute.data_type, attribute.name))
    if len(attribute.data):
        available = {prop.identifier for prop in attribute.data[0].bl_rna.properties}
        for prop, _width, _kind in plan:
            if prop not in available:
                _unsupported("Attribute %s does not expose %s" % (attribute.name, prop))
    return plan


def _attribute_field_digest(attribute, prop, width, kind):
    count = len(attribute.data)
    digest = hashlib.sha256()
    if kind == "s":
        digest.update(b"S")
        digest.update(struct.pack("<I", count))
        for entry in attribute.data:
            value = getattr(entry, prop)
            if isinstance(value, (bytes, bytearray)):
                raw = bytes(value)
            else:
                raw = str(value).encode("utf-8")
            digest.update(struct.pack("<I", len(raw)))
            digest.update(raw)
        return digest.hexdigest(), 0
    if kind == "b":
        values = array("b", [0]) * (count * width)
    elif kind == "i":
        values = array("i", [0]) * (count * width)
    else:
        values = array("f", [0.0]) * (count * width)
    try:
        attribute.data.foreach_get(prop, values)
    except Exception as exc:
        _unsupported("Cannot bulk-read attribute %s.%s: %s" % (attribute.name, prop, exc))
    digest.update(struct.pack("<II", width, count))
    digest.update(values.tobytes())
    non_finite = _count_nonfinite(values) if kind == "f" else 0
    return digest.hexdigest(), non_finite


def _attribute_state(attribute):
    plan = _attribute_plan(attribute)
    count = len(attribute.data)
    fields = []
    overall = hashlib.sha256()
    _hash_update_text(overall, attribute.name)
    _hash_update_text(overall, attribute.domain)
    _hash_update_text(overall, attribute.data_type)
    overall.update(struct.pack("<I", count))
    non_finite = 0
    for prop, width, kind in plan:
        digest, field_non_finite = _attribute_field_digest(attribute, prop, width, kind)
        non_finite += field_non_finite
        field = {"property": prop, "width": width, "kind": kind, "sha256": digest}
        fields.append(field)
        _hash_update_text(overall, prop)
        _hash_update_text(overall, kind)
        overall.update(struct.pack("<I", width))
        _hash_update_text(overall, digest)
    return {
        "name": attribute.name,
        "domain": attribute.domain,
        "data_type": attribute.data_type,
        "count": count,
        "fields": fields,
        "non_finite": non_finite,
        "sha256": overall.hexdigest(),
    }


def _attributes_state(mesh):
    uv_names = {layer.name for layer in mesh.uv_layers}
    rows = []
    for attribute in mesh.attributes:
        name = attribute.name
        if name in uv_names or _skip_attribute(name, uv_names):
            continue
        rows.append(_attribute_state(attribute))
    rows.sort(key=lambda row: (row["name"], row["domain"], row["data_type"]))
    return rows


def _material_index_state(mesh):
    count = len(mesh.polygons)
    values = array("i", [0]) * count
    if count:
        mesh.polygons.foreach_get("material_index", values)
    digest = _array_digest(values, "polygon_material_index", "i")
    return {"count": count, "sha256": digest}


def _vertex_groups_state(obj):
    definitions = [
        {"index": int(group.index), "name": group.name, "lock_weight": bool(group.lock_weight)}
        for group in obj.vertex_groups
    ]
    digest = hashlib.sha256()
    digest.update(struct.pack("<I", len(obj.data.vertices)))
    digest.update(struct.pack("<I", len(definitions)))
    max_groups = 0
    dangling = 0
    non_finite = 0
    group_count = len(definitions)
    for vertex in obj.data.vertices:
        entries = []
        for element in vertex.groups:
            group_index = int(element.group)
            weight = float(element.weight)
            if group_index >= group_count:
                dangling += 1
            if not math.isfinite(weight):
                non_finite += 1
            entries.append((group_index, weight))
        entries.sort()
        max_groups = max(max_groups, len(entries))
        digest.update(struct.pack("<II", int(vertex.index), len(entries)))
        for group_index, weight in entries:
            digest.update(struct.pack("<id", group_index, weight))
    if non_finite:
        _invalid("Vertex weights contain %d non-finite values" % non_finite)
    return {
        "definitions": definitions,
        "vertices": len(obj.data.vertices),
        "max_groups_per_vertex": max_groups,
        "dangling_group_indices": dangling,
        "sha256": digest.hexdigest(),
    }


def _shape_key_state(mesh, keys):
    if keys is None:
        return None
    blocks = []
    for index, block in enumerate(keys.key_blocks):
        if len(block.data) != len(mesh.vertices):
            _invalid("Shape key %s/%s has %d points for %d vertices" %
                     (keys.name, block.name, len(block.data), len(mesh.vertices)))
        values = array("f", [0.0]) * (len(block.data) * 3)
        try:
            block.data.foreach_get("co", values)
        except Exception as exc:
            _unsupported("Cannot bulk-read shape key %s/%s: %s" %
                         (keys.name, block.name, exc))
        non_finite = _count_nonfinite(values)
        if non_finite:
            _invalid("Shape key %s/%s contains %d non-finite coordinates" %
                     (keys.name, block.name, non_finite))
        relative = getattr(block, "relative_key", None)
        blocks.append({
            "index": index,
            "name": block.name,
            "value": _json_number(block.value),
            "mute": bool(block.mute),
            "lock_shape": bool(block.lock_shape),
            "relative_key": relative.name if relative else None,
            "vertex_group": block.vertex_group,
            "slider_min": _json_number(block.slider_min),
            "slider_max": _json_number(block.slider_max),
            "interpolation": block.interpolation,
            "frame": _json_number(block.frame),
            "points": len(block.data),
            "non_finite": non_finite,
            "co_sha256": _array_digest(values, "shape_co", "f", 3),
        })
    reference = getattr(keys, "reference_key", None)
    return {
        "identity": _identity(keys),
        "users": int(keys.users),
        "use_fake_user": bool(keys.use_fake_user),
        "reference_key": reference.name if reference else None,
        "use_relative": bool(keys.use_relative),
        "eval_time": _json_number(keys.eval_time),
        "blocks": blocks,
        "custom_properties": _idprops_summary(keys),
        "animation": _animation_state(keys.animation_data),
    }


def _mesh_state(mesh):
    return {
        "identity": _identity(mesh),
        "users": int(mesh.users),
        "use_fake_user": bool(mesh.use_fake_user),
        "counts": {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "loops": len(mesh.loops),
            "polygons": len(mesh.polygons),
        },
        "materials": [_identity(material) for material in mesh.materials],
        "material_index": _material_index_state(mesh),
        "attributes": _attributes_state(mesh),
        "custom_properties": _idprops_summary(mesh),
        "animation": _animation_state(mesh.animation_data),
    }


def _material_slots_state(obj):
    slots = []
    for slot in obj.material_slots:
        if slot.link not in {"DATA", "OBJECT"}:
            _unsupported("Object %s has uncertified material-slot link %s" %
                         (obj.name, slot.link))
        slots.append({
            "slot_index": int(slot.slot_index),
            "link": slot.link,
            "material": _identity(slot.material),
        })
    return slots


def _object_state(obj):
    mesh = obj.data
    parent = obj.parent
    return {
        "identity": _identity(obj),
        "data": _identity(mesh),
        "use_fake_user": bool(obj.use_fake_user),
        "parent": _identity(parent),
        "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone,
        "matrix_parent_inverse": [
            [_json_number(value) for value in row] for row in obj.matrix_parent_inverse
        ],
        "active_shape_key_index": int(obj.active_shape_key_index),
        "active_shape_key": obj.active_shape_key.name if obj.active_shape_key else None,
        "show_only_shape_key": bool(obj.show_only_shape_key),
        "use_shape_key_edit_mode": bool(getattr(obj, "use_shape_key_edit_mode", False)),
        "constraints": [_constraint_state(c) for c in obj.constraints],
        "vertex_groups": _vertex_groups_state(obj),
        "modifiers": [_modifier_state(modifier) for modifier in obj.modifiers],
        "material_slots": _material_slots_state(obj),
        "custom_properties": _idprops_summary(obj),
        "animation": _animation_state(obj.animation_data),
        "mesh": _mesh_state(mesh),
        "shape_keys": _shape_key_state(mesh, mesh.shape_keys),
    }


def _check_fcurve_modifiers(fcurve, owner):
    for modifier in fcurve.modifiers:
        try:
            _fcurve_modifier_state(modifier)
        except IntegrityError:
            raise
        except Exception as exc:
            _unsupported("Cannot inspect FCurve modifier on %s: %s" % (owner, exc))


def _check_action(action, owner, allow_linked=False):
    if action is None:
        return
    if (action.library or action.override_library) and not allow_linked:
        _unsupported("Action %s used by %s is linked/overridden; full-scene protection must cover it" %
                     (action.name, owner))
    if getattr(action, "is_missing", False):
        _invalid("Action %s used by %s is missing" % (action.name, owner))
    for slot in action.slots:
        _slot_identifier(slot)
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type != "KEYFRAME" or not hasattr(strip, "channelbags"):
                _unsupported("Action %s/%s has uncertified strip type %s" %
                             (action.name, layer.name, strip.type))
            for bag in strip.channelbags:
                _slot_identifier(bag.slot)
                for fcurve in bag.fcurves:
                    _check_fcurve_modifiers(fcurve, action.name)
    if not action.layers:
        try:
            legacy = list(action.fcurves)
        except Exception as exc:
            _unsupported("Cannot inspect legacy FCurves of action %s: %s" % (action.name, exc))
        for fcurve in legacy:
            _check_fcurve_modifiers(fcurve, action.name)


def _check_driver(fcurve, owner):
    driver = fcurve.driver
    driver_type = driver.type
    if driver_type == "SCRIPTED":
        simple = bool(getattr(driver, "is_simple_expression", False))
        if not simple:
            _unsupported(
                "Driver %s[%d] on %s uses a non-simple/Python expression; "
                "auto-execution is never enabled and this driver is not certified" %
                (fcurve.data_path, fcurve.array_index, owner))
    if not bool(getattr(driver, "is_valid", False)):
        _unsupported("Driver %s[%d] on %s is invalid" %
                     (fcurve.data_path, fcurve.array_index, owner))
    for variable in driver.variables:
        for target in variable.targets:
            target_id = getattr(target, "id", None)
            if target_id is not None and not _is_id(target_id):
                _unsupported("Driver %s on %s has an uncertified variable target" %
                             (fcurve.data_path, owner))
    _check_fcurve_modifiers(fcurve, owner)


def _check_animation_data(animation_data, owner, allow_linked_actions=False):
    if animation_data is None:
        return
    if animation_data.use_tweak_mode:
        _unsupported("NLA tweak mode on %s is not a stable protected state" % owner)
    action = animation_data.action
    _check_action(action, owner, allow_linked=allow_linked_actions)
    if action is not None and len(action.slots):
        active_slot = getattr(animation_data, "action_slot", None)
        if active_slot is None:
            _unsupported("Action %s on %s has slots but no active slot" %
                         (action.name, owner))
        _slot_identifier(active_slot)
    for track in animation_data.nla_tracks:
        for strip in track.strips:
            if getattr(strip, "strips", None):
                _unsupported("Nested NLA strips on %s are not certified" % owner)
            strip_action = getattr(strip, "action", None)
            if strip_action is None:
                _unsupported("NLA strip %s on %s has no action" % (strip.name, owner))
            _check_action(strip_action, owner, allow_linked=allow_linked_actions)
            if len(strip_action.slots):
                _slot_identifier(getattr(strip, "action_slot", None))
            try:
                for fcurve in strip.fcurves:
                    _check_fcurve_modifiers(fcurve, owner)
            except Exception as exc:
                _unsupported("Cannot inspect NLA strip FCurves on %s: %s" % (owner, exc))
            try:
                for modifier in strip.modifiers:
                    _fcurve_modifier_state(modifier)
            except IntegrityError:
                raise
            except Exception as exc:
                _unsupported("Cannot inspect NLA strip modifiers on %s: %s" % (owner, exc))
    for fcurve in animation_data.drivers:
        _check_driver(fcurve, owner)


def _check_modifier(modifier, owner):
    if modifier.type == "NODES" and not _is_shading_nodes_modifier(modifier):
        _unsupported(
            "Geometry Nodes modifier %s on %s is not a certified native shading tree; "
            "its node-group content is not certified" % (modifier.name, owner))
    # All other modifier types are accepted only if the bounded writer can
    # represent their writable settings/references; final surface is separate.
    _modifier_state(modifier)


def _check_attribute(attribute, uv_names):
    if _skip_attribute(attribute.name, uv_names):
        return
    _attribute_plan(attribute)


def _check_object(obj, writable=True):
    if not isinstance(obj, bpy.types.Object):
        _unsupported("Target is not an Object")
    if obj.type != "MESH":
        _unsupported("Target %s is not a MESH" % obj.name)
    if obj.mode != "OBJECT":
        _conflict("UV workflow requires Object Mode; target %s is in %s mode. "
                  "Switch to Object Mode; for CLI use, save the working copy after switching." %
                  (obj.name, obj.mode))
    if obj.library or obj.override_library:
        _unsupported("Target %s is linked/overridden; local meshes only" % obj.name)
    mesh = obj.data
    if mesh is None:
        _invalid("Target %s has no mesh data" % obj.name)
    if mesh.library or mesh.override_library:
        _unsupported("Mesh %s on %s is linked/overridden" % (mesh.name, obj.name))
    if writable and mesh.users - int(bool(mesh.use_fake_user)) > 1:
        _unsupported("Mesh %s on %s is shared by multiple objects; single-user mesh required" %
                     (mesh.name, obj.name))
    keys = mesh.shape_keys
    if keys is not None:
        if keys.library or keys.override_library:
            _unsupported("Shape-key datablock %s on %s is linked/overridden" %
                         (keys.name, obj.name))
        if writable and keys.users - int(bool(keys.use_fake_user)) > 1:
            _unsupported("Shape-key datablock %s is shared" % keys.name)
        if not len(keys.key_blocks):
            _invalid("Shape-key datablock %s has no blocks" % keys.name)
        group_names = {group.name for group in obj.vertex_groups}
        block_names = {block.name for block in keys.key_blocks}
        for block in keys.key_blocks:
            if len(block.data) != len(mesh.vertices):
                _invalid("Shape key %s/%s has %d points for %d vertices" %
                         (keys.name, block.name, len(block.data), len(mesh.vertices)))
            relative = getattr(block, "relative_key", None)
            if relative is not None and relative.name not in block_names:
                _invalid("Shape key %s/%s has a foreign relative key" %
                         (keys.name, block.name))
            if block.vertex_group and block.vertex_group not in group_names:
                _unsupported("Shape key %s/%s references missing vertex group %s" %
                             (keys.name, block.name, block.vertex_group))
    # Driver safety is established before any drivable property value is read.
    _check_animation_data(obj.animation_data, obj.name)
    _check_animation_data(mesh.animation_data, "mesh %s" % mesh.name)
    if keys is not None:
        _check_animation_data(keys.animation_data, "shape keys %s" % keys.name)
    for constraint in obj.constraints:
        _constraint_state(constraint)
    if obj.parent_type not in {"OBJECT", "BONE"}:
        _unsupported("Object %s has uncertified parent type %s" %
                     (obj.name, obj.parent_type))
    if obj.parent_type == "BONE" and (obj.parent is None or obj.parent.type != "ARMATURE"):
        _invalid("Object %s uses bone parenting without an armature parent" % obj.name)
    if obj.parent_type == "BONE" and not obj.parent_bone:
        _invalid("Object %s uses bone parenting without a parent bone" % obj.name)
    for modifier in obj.modifiers:
        _check_modifier(modifier, obj.name)
    _material_slots_state(obj)
    uv_names = {layer.name for layer in mesh.uv_layers}
    for attribute in mesh.attributes:
        if attribute.name in uv_names:
            continue
        _check_attribute(attribute, uv_names)


def local_supported(objects, writable=True):
    """Assert the certified local deformation-state boundary; return ``None``.

    The check is read-only and never evaluates the dependency graph.  Non-simple
    scripted drivers are refused even when Blender's auto-execution setting is
    enabled, so an unchanged driver definition is never treated as proof that a
    script is safe.
    """
    try:
        items = list(objects)
    except TypeError:
        _unsupported("objects must be an iterable of MESH objects")
    seen = set()
    for obj in items:
        if not isinstance(obj, bpy.types.Object):
            _unsupported("objects contains a non-Object value")
        if obj.name in seen:
            _conflict("Duplicate target object name %s" % obj.name)
        seen.add(obj.name)
        _check_object(obj, writable=writable)
    _related_rigs_checked(items)
    return None


def snapshot(objects, writable=False):
    """Return a deterministic, JSON-safe deformation-state snapshot.

    The caller is expected to combine this with ``core.protection`` for base
    geometry, existing UV layers, pins, seams and the current world matrix.
    """
    items = list(objects)
    local_supported(items, writable=writable)
    rows = {}
    for obj in items:
        rows[obj.name] = _object_state(obj)
    return {"schema": SCHEMA, "objects": rows, "dependencies": _dependencies_state(items)}
