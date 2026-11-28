"""Apply named edits to the selected hard-surface tool's saved Blender RNA controls."""

import json
import math
import struct

from protocol import Failure
from scene_contract import validate as validate_value


OPERATION = "configure-tool"
CONTROLS = {
    "repeat": {"type": "integer", "minimum": 1, "maximum": 64},
    "allow_expansion": {"type": "boolean"},
    "use_protect": {"type": "boolean"},
    "protect_group": {"type": "string"},
}


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or changes.keys() - {"parameters", "controls"}:
        raise Failure("INVALID_REQUEST", "changes must contain parameters and/or controls")
    for name, patch in changes.items():
        if not isinstance(patch, dict) or not patch:
            raise Failure("INVALID_REQUEST", f"changes.{name} must be a nonempty object")
    for name, value in changes.get("controls", {}).items():
        if name not in CONTROLS:
            raise Failure("INVALID_REQUEST", f"Unknown execution control: {name}")
        validate_value(value, CONTROLS[name], name)


def _schema(spec):
    # Released tools use both Blender descriptor names and JSON Schema names.
    schema = {k: spec[k] for k in ("type", "const", "enum", "uniqueItems") if k in spec}
    for source, target in (("min", "minimum"), ("max", "maximum"), ("min_items", "minItems"),
                           ("max_items", "maxItems"), ("minItems", "minItems"), ("maxItems", "maxItems")):
        if source in spec:
            schema[target] = spec[source]
    if "items" in spec:
        schema["items"] = _schema(spec["items"])
    if "properties" in spec:
        members = spec["properties"]
        schema.update(properties={k: _schema(v) for k, v in members.items()}, additionalProperties=False,
                      required=[k for k, v in members.items() if v.get("required")])
    return {"oneOf": [schema, {"type": "null"}]} if spec.get("nullable") else schema


def _widget_value(value, kind):
    if kind == "number":
        try:
            result = struct.unpack("f", struct.pack("f", value))[0]
        except (OverflowError, struct.error) as exc:
            raise Failure("INVALID_REQUEST", "Value exceeds Blender floating point range") from exc
        if not math.isfinite(result):
            raise Failure("INVALID_REQUEST", "Value exceeds Blender floating point range")
        return result
    return value


def _widget_changes(tool, parameters):
    from hard_surface_workflow_addon.panel.settings import iter_bindings

    bindings = {binding.name: binding for binding in iter_bindings(tool)}
    unknown = parameters.keys() - bindings.keys()
    if unknown:
        raise Failure("INVALID_REQUEST", f"Unknown parameters for {tool}: {', '.join(sorted(unknown))}")
    updates = {}
    for name, value in parameters.items():
        binding = bindings[name]
        validate_value(value, _schema(binding.spec), name)
        if binding.kind == "scalar":
            if binding.null_prop:
                updates[binding.null_prop] = value is None
            if value is not None:
                updates[binding.prop] = _widget_value(value, binding.item_type)
        elif binding.kind == "flags":
            updates.update({prop: choice in value for prop, choice, *_ in binding.choices})
        elif binding.kind == "fixed":
            if len(binding.items) != len(value):
                raise Failure("INVALID_REQUEST", f"Parameter {name} requires exactly {len(binding.items)} values")
            for (prop, _), item in zip(binding.items, value, strict=True):
                updates[prop] = _widget_value(item, binding.item_type)
        elif binding.kind == "json":
            updates[binding.prop] = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        elif binding.kind != "const":
            raise Failure("UNSUPPORTED", f"Unknown native parameter binding: {binding.kind}")
    return updates


def execute(context, request):
    settings = context.scene.hsw_settings
    tool = request["tool_id"]
    if settings.tool != tool:
        raise Failure("CONFLICT", f"The saved panel has selected {settings.tool!r}, expected {tool!r}")
    changes = request["changes"]
    controls = changes.get("controls", {})
    updates = _widget_changes(tool, changes.get("parameters", {}))
    if {"use_protect", "protect_group"} & controls.keys() and controls.get("use_protect", settings.use_protect):
        target = settings.target_object or context.active_object
        name = controls.get("protect_group", settings.protect_group)
        if target is None or target.type != "MESH" or target.vertex_groups.get(name) is None:
            raise Failure("CONFLICT", "Enabling protection requires an existing vertex group on the panel target")
    try:
        for prop, value in updates.items():
            setattr(settings.params, prop, value)
        for prop, value in controls.items():
            setattr(settings, prop, value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise Failure("INVALID_REQUEST", f"Blender rejected the parameter value: {exc}") from exc
    return {"id": "Scene.hsw_settings", "method": "RNA", "result": ["FINISHED"], "arguments": changes}


def verify(before, after, request):
    changes = request["changes"]
    parameters = changes.get("parameters", {})
    controls = changes.get("controls", {})
    raw = _widget_changes(request["tool_id"], parameters)
    old, new = before["settings"], after["settings"]
    if (new["params"] != {**old["params"], **raw} or new["values"] != {**old["values"], **controls}
            or after["profile"] != before["profile"] or after["rna"] != before["rna"]):
        raise Failure("VALIDATION_FAILED", "Requested values or other saved tool and panel fields changed unexpectedly")
    return {"kind": OPERATION, "tool": request["tool_id"], "parameters": list(parameters),
            "controls": {key: new["values"][key] for key in controls}, "tool_parameters": new["tool_params"]}
