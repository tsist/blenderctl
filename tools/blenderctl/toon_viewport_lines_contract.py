"""Strict JSON contract for the narrow Toon viewport-lines candidate.

This module is pure Python and does not import Blender. It is shared by the
parent supervisor and the Blender worker. It only validates request shape,
identity and numeric bounds; scene identity and current-author binding are
checked in the worker.
"""

import math
import re

from protocol import Failure


OPERATIONS = ("inspect", "edit", "render-camera")
REQUEST_SCHEMA = "toon.viewport-lines-request.v1"
CATEGORIES = (
    "outline",
    "object",
    "intersection",
    "smooth",
    "material",
    "selected_edge",
    "normal_angle",
    "wireframe",
)
GATES = (
    "disable_intersection",
    "draw_hidden_lines",
    "draw_hidden_lines_of_targets",
    "mask_hidden_lines_of_targets",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def name(value, limit=255):
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all(ord(char) >= 32 for char in value)
    )


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def ref(value, label):
    require(
        isinstance(value, dict) and set(value) == {"name", "library"},
        "INVALID_REQUEST",
        label + " requires exactly name and library",
    )
    require(name(value["name"]), "INVALID_REQUEST", label + " has an invalid name")
    require(
        value["library"] is None,
        "UNSUPPORTED",
        label + " is local-only in viewport-lines v1; library must be null",
    )
    return {"name": value["name"], "library": None}


def refs(value, label, *, minimum=0, maximum=4096):
    require(
        isinstance(value, list) and minimum <= len(value) <= maximum,
        "INVALID_REQUEST",
        label + " requires a list of " + str(minimum) + ".." + str(maximum) + " identities",
    )
    result = [ref(item, label) for item in value]
    keys = [item["name"] for item in result]
    require(len(keys) == len(set(keys)), "INVALID_REQUEST", label + " contains duplicate identities")
    return result


def scope_value(value):
    require(
        isinstance(value, dict) and set(value) <= {"groups", "objects", "collections", "materials"},
        "INVALID_REQUEST",
        "scope accepts groups/objects/collections/materials only",
    )
    groups = value.get("groups")
    require(
        isinstance(groups, list) and 1 <= len(groups) <= 32 and all(name(item) for item in groups),
        "INVALID_REQUEST",
        "scope.groups requires 1..32 unique group IDs or labels",
    )
    require(len(groups) == len(set(groups)), "INVALID_REQUEST", "scope.groups contains duplicates")
    return {
        "groups": list(groups),
        "objects": refs(value.get("objects", []), "scope.objects", maximum=4096),
        "collections": refs(value.get("collections", []), "scope.collections", maximum=4096),
        "materials": refs(value.get("materials", []), "scope.materials", maximum=4096),
    }


def context_value(value):
    require(
        isinstance(value, dict) and set(value) == {"scene", "view_layer", "frame"},
        "INVALID_REQUEST",
        "context requires exactly scene, view_layer and frame",
    )
    require(name(value["scene"]) and name(value["view_layer"]), "INVALID_REQUEST", "Invalid scene or view layer name")
    require(
        type(value["frame"]) is int and -1048574 <= value["frame"] <= 1048574,
        "INVALID_REQUEST",
        "context.frame must be an integer within the Blender frame range",
    )
    return dict(value)


def group_patch(value):
    require(
        isinstance(value, dict) and {"group"} <= set(value) <= {"group", "width_scale", "opacity", "enabled"},
        "INVALID_REQUEST",
        "A group patch accepts group, width_scale, opacity and enabled",
    )
    require(name(value["group"]), "INVALID_REQUEST", "Invalid group identity or label")
    require(
        any(key in value for key in ("width_scale", "opacity", "enabled")),
        "INVALID_REQUEST",
        "A group patch needs at least one value",
    )
    result = {"group": value["group"]}
    if "width_scale" in value:
        require(
            finite(value["width_scale"]) and 0 <= value["width_scale"] <= 8,
            "INVALID_REQUEST",
            "group.width_scale must be finite in 0..8",
        )
        result["width_scale"] = float(value["width_scale"])
    if "opacity" in value:
        require(
            finite(value["opacity"]) and 0 <= value["opacity"] <= 1,
            "INVALID_REQUEST",
            "group.opacity must be finite in 0..1",
        )
        result["opacity"] = float(value["opacity"])
    if "enabled" in value:
        require(type(value["enabled"]) is bool, "INVALID_REQUEST", "group.enabled must be boolean")
        result["enabled"] = value["enabled"]
    return result


def brush_patch(value):
    allowed = {
        "group",
        "actual",
        "channel",
        "role",
        "native_size",
        "color",
        "blend_amount",
    }
    require(
        isinstance(value, dict) and {"group"} <= set(value) <= allowed,
        "INVALID_REQUEST",
        "A brush patch accepts group, actual or channel/role, native_size, color and blend_amount",
    )
    require(name(value["group"]), "INVALID_REQUEST", "Invalid brush group identity or label")
    actual = value.get("actual")
    channel = value.get("channel")
    role = value.get("role")
    require(
        (actual is None) != (channel is None and role is None),
        "INVALID_REQUEST",
        "A brush patch needs either actual or channel+role",
    )
    if actual is not None:
        require(name(actual, 256), "INVALID_REQUEST", "brush.actual must be a nonempty string")
        require(channel is None and role is None, "INVALID_REQUEST", "actual cannot be combined with channel/role")
    else:
        require(
            channel in ("VISIBLE", "HIDDEN") and name(role, 64),
            "INVALID_REQUEST",
            "brush.channel must be VISIBLE or HIDDEN and brush.role nonempty",
        )
    require(
        any(key in value for key in ("native_size", "color", "blend_amount")),
        "INVALID_REQUEST",
        "A brush patch needs at least one value",
    )
    result = dict(value)
    if "native_size" in value:
        require(
            finite(value["native_size"]) and 0.01 <= value["native_size"] <= 1024,
            "INVALID_REQUEST",
            "brush.native_size must be finite in 0.01..1024",
        )
        result["native_size"] = float(value["native_size"])
    if "color" in value:
        color = value["color"]
        require(
            isinstance(color, list)
            and len(color) == 3
            and all(finite(item) and 0 <= item <= 1 for item in color),
            "INVALID_REQUEST",
            "brush.color must be three finite values in 0..1",
        )
        result["color"] = [float(item) for item in color]
    if "blend_amount" in value:
        require(
            finite(value["blend_amount"]) and 0 <= value["blend_amount"] <= 1,
            "INVALID_REQUEST",
            "brush.blend_amount must be finite in 0..1",
        )
        result["blend_amount"] = float(value["blend_amount"])
    return result


def selector_patch(value, scope):
    allowed = {"group", "enabled", "objects", "collections", "materials", "initialize_empty"}
    require(
        isinstance(value, dict) and {"group"} <= set(value) <= allowed,
        "INVALID_REQUEST",
        "A selector patch accepts group, enabled, objects, collections, materials or initialize_empty",
    )
    require(name(value["group"]), "INVALID_REQUEST", "Invalid selector group identity or label")
    if "initialize_empty" in value:
        require(value.keys() == {"group", "initialize_empty"} and value["initialize_empty"] is True,
                "INVALID_REQUEST", "initialize_empty=true only takes the group ID")
        return dict(value)
    require(
        any(key in value for key in ("enabled", "objects", "collections", "materials")),
        "INVALID_REQUEST",
        "A selector patch needs at least one field",
    )
    result = {"group": value["group"]}
    if "enabled" in value:
        require(type(value["enabled"]) is bool, "INVALID_REQUEST", "selector.enabled must be boolean")
        result["enabled"] = value["enabled"]
    for kind in ("objects", "collections", "materials"):
        if kind not in value:
            continue
        items = refs(value[kind], "selector." + kind, maximum=4096)
        allowed_names = {item["name"] for item in scope[kind]}
        for item in items:
            require(
                item["name"] in allowed_names,
                "INVALID_REQUEST",
                "selector."
                + kind
                + " identity is outside scope."
                + kind
                + ": "
                + item["name"],
            )
        result[kind] = items
    return result


def override_value(value, label):
    require(
        isinstance(value, dict) and set(value) == {"on", "color", "amount"},
        "INVALID_REQUEST",
        label + " requires on, color and amount",
    )
    require(type(value["on"]) is bool, "INVALID_REQUEST", label + ".on must be boolean")
    color = value["color"]
    require(
        isinstance(color, list)
        and len(color) == 3
        and all(finite(item) and 0 <= item <= 1 for item in color),
        "INVALID_REQUEST",
        label + ".color must be three finite values in 0..1",
    )
    require(
        finite(value["amount"]) and 0 <= value["amount"] <= 1,
        "INVALID_REQUEST",
        label + ".amount must be finite in 0..1",
    )
    return {"on": value["on"], "color": [float(item) for item in color], "amount": float(value["amount"])}


def line_function_container(value, scope):
    allowed = {
        "id",
        "materials",
        "draw_objects",
        "draw_materials",
        "mask_objects",
        "mask_materials",
        *GATES,
        "overrides",
    }
    require(
        isinstance(value, dict) and {"id"} <= set(value) <= allowed,
        "INVALID_REQUEST",
        "A line-function container has an unknown field",
    )
    require(name(value["id"], 128), "INVALID_REQUEST", "Invalid line-function container ID")
    result = {"id": value["id"]}
    if "materials" in value:
        materials = refs(value["materials"], "line_functions.materials", minimum=1, maximum=64)
        allowed_materials = {item["name"] for item in scope["materials"]}
        for item in materials:
            require(
                item["name"] in allowed_materials,
                "INVALID_REQUEST",
                "line-function material is outside scope.materials: " + item["name"],
            )
        result["materials"] = materials
    for field in ("draw_objects", "draw_materials", "mask_objects", "mask_materials"):
        if field in value:
            require(
                value[field] in ([], None),
                "UNSUPPORTED",
                "viewport-lines v1 supports material line-function targets only; "
                + field
                + " must be empty or absent",
            )
    for gate in GATES:
        if gate in value:
            require(type(value[gate]) is bool, "INVALID_REQUEST", gate + " must be boolean")
            result[gate] = value[gate]
    if "overrides" in value:
        require(isinstance(value["overrides"], dict), "INVALID_REQUEST", "overrides must be an object")
        require(
            set(value["overrides"]) <= set(CATEGORIES),
            "INVALID_REQUEST",
            "overrides contains an unknown category",
        )
        result["overrides"] = {
            key: override_value(item, "overrides." + key)
            for key, item in value["overrides"].items()
        }
    return result


def line_functions_patch(value, scope):
    allowed = {"containers", "remove"}
    require(
        isinstance(value, dict) and set(value) <= allowed and value,
        "INVALID_REQUEST",
        "line_functions accepts containers and/or remove",
    )
    result = {}
    if "containers" in value:
        require(
            isinstance(value["containers"], list) and 1 <= len(value["containers"]) <= 64,
            "INVALID_REQUEST",
            "line_functions.containers requires 1..64 entries",
        )
        result["containers"] = [line_function_container(item, scope) for item in value["containers"]]
        ids = [item["id"] for item in result["containers"]]
        require(len(ids) == len(set(ids)), "INVALID_REQUEST", "Duplicate line-function container ID")
    if "remove" in value:
        require(
            isinstance(value["remove"], list)
            and 1 <= len(value["remove"]) <= 64
            and all(name(item, 128) for item in value["remove"]),
            "INVALID_REQUEST",
            "line_functions.remove requires 1..64 IDs",
        )
        require(
            len(value["remove"]) == len(set(value["remove"])),
            "INVALID_REQUEST",
            "Duplicate line-function removal ID",
        )
        if "containers" in result:
            require(
                not set(value["remove"]) & {item["id"] for item in result["containers"]},
                "INVALID_REQUEST",
                "A line-function container cannot be updated and removed together",
            )
        result["remove"] = list(value["remove"])
    return result


def patch_value(value, scope):
    allowed = {"groups", "brushes", "selectors", "selector_order", "line_functions"}
    require(
        isinstance(value, dict) and set(value) <= allowed and value,
        "INVALID_REQUEST",
        "patch accepts groups, brushes, selectors, selector_order and line_functions",
    )
    result = {}
    if "selector_order" in value:
        order = value["selector_order"]
        require(order is None or (isinstance(order, list) and 0 < len(order) <= 32 and
                all(name(key) for key in order) and len(set(order)) == len(order)),
                "INVALID_REQUEST", "selector_order requires a list of all groups, or null to restore priority")
        result["selector_order"] = order
    if "groups" in value:
        require(
            isinstance(value["groups"], list) and 1 <= len(value["groups"]) <= 32,
            "INVALID_REQUEST",
            "patch.groups requires 1..32 entries",
        )
        result["groups"] = [group_patch(item) for item in value["groups"]]
        groups = [item["group"] for item in result["groups"]]
        require(len(groups) == len(set(groups)), "INVALID_REQUEST", "Duplicate group patch")
    if "brushes" in value:
        require(
            isinstance(value["brushes"], list) and 1 <= len(value["brushes"]) <= 64,
            "INVALID_REQUEST",
            "patch.brushes requires 1..64 entries",
        )
        result["brushes"] = [brush_patch(item) for item in value["brushes"]]
        brushes = [
            (item["group"], item.get("actual") or (item.get("channel"), item.get("role")))
            for item in result["brushes"]
        ]
        require(len(brushes) == len(set(brushes)), "INVALID_REQUEST", "Duplicate brush patch")
    if "selectors" in value:
        require(
            isinstance(value["selectors"], list) and 1 <= len(value["selectors"]) <= 32,
            "INVALID_REQUEST",
            "patch.selectors requires 1..32 entries",
        )
        result["selectors"] = [selector_patch(item, scope) for item in value["selectors"]]
        groups = [item["group"] for item in result["selectors"]]
        require(len(groups) == len(set(groups)), "INVALID_REQUEST", "Duplicate selector group patch")
    if "line_functions" in value:
        result["line_functions"] = line_functions_patch(value["line_functions"], scope)
    return result


def normalize_request(value, editions):
    require(isinstance(value, dict), "INVALID_REQUEST", "Request must be a JSON object")
    allowed = {
        "schema",
        "operation",
        "edition",
        "file",
        "expected_sha256",
        "expected_author_sha256",
        "context",
        "scope",
        "patch",
        "frames",
        "elements",
    }
    require(set(value) <= allowed, "INVALID_REQUEST", "Unknown request field")
    if "schema" in value:
        require(value["schema"] == REQUEST_SCHEMA, "INVALID_REQUEST", "Unknown request schema")
    require({"operation", "edition", "file", "expected_sha256", "context"} <= set(value),
            "INVALID_REQUEST", "Missing viewport-lines request fields")
    operation = value["operation"]
    require(operation in OPERATIONS, "INVALID_REQUEST", "Unknown viewport-lines operation")
    require(
        isinstance(value["edition"], str) and value["edition"] in editions,
        "UNSUPPORTED",
        "Unknown Toon edition",
    )
    require(
        isinstance(value["file"], str) and value["file"].lower().endswith(".blend"),
        "INVALID_REQUEST",
        "file must be a .blend path",
    )
    require(
        isinstance(value["expected_sha256"], str) and SHA256.fullmatch(value["expected_sha256"]),
        "INVALID_REQUEST",
        "expected_sha256 must be lowercase SHA-256",
    )
    if "expected_author_sha256" in value:
        require(
            isinstance(value["expected_author_sha256"], str)
            and SHA256.fullmatch(value["expected_author_sha256"]),
            "INVALID_REQUEST",
            "expected_author_sha256 must be lowercase SHA-256",
        )
    if operation == "edit":
        require("expected_author_sha256" in value, "INVALID_REQUEST", "edit requires expected_author_sha256")
        require("patch" in value, "INVALID_REQUEST", "edit requires patch")
    else:
        require("patch" not in value, "INVALID_REQUEST", "Only edit accepts patch")
    result = {
        "operation": operation,
        "edition": value["edition"],
        "file": value["file"],
        "expected_sha256": value["expected_sha256"],
        "context": context_value(value["context"]),
    }
    if "expected_author_sha256" in value:
        result["expected_author_sha256"] = value["expected_author_sha256"]
    if operation == "render-camera":
        require("scope" not in value and "expected_author_sha256" in value, "INVALID_REQUEST",
                "render-camera uses the saved camera and line scope; bind expected_author_sha256")
        if "frames" in value:
            frames = value["frames"]
            require(isinstance(frames, list) and 0 < len(frames) <= 96 and
                    all(type(f) is int and -1048574 <= f <= 1048574 for f in frames) and len(set(frames)) == len(frames),
                    "INVALID_REQUEST", "Expected 1..96 unique integer frames")
            result["frames"] = frames
        if "elements" in value:
            elements = value["elements"]
            require(isinstance(elements, list) and elements and all(isinstance(e, dict) for e in elements),
                    "INVALID_REQUEST", "Expected a nonempty list of named EXR elements")
            result["elements"] = elements
        return result
    require(not ({"frames","elements"} & value.keys()) and "scope" in value,
            "INVALID_REQUEST", "Authoring requires scope; frames and elements belong to render-camera")
    result["scope"] = scope_value(value["scope"])
    if operation == "edit":
        result["patch"] = patch_value(value["patch"], result["scope"])
    return result
