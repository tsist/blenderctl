# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable JSON templates; all scene and physical resource bindings are explicit."""
from copy import deepcopy
import json
from pathlib import Path
from .contract import (WorkflowError, _fail, _validate, obj, ID, NAME, IMAGE,
                       SHA, LAYER_V2, normalize_manifest, PREVIEW,
                       MANIFEST_V2_SCHEMA)

RESOURCE_ID = {**ID, "maxLength": 96}
REF = obj({"resource": RESOURCE_ID, "color_space": {"enum": ["sRGB", "Non-Color"]}}, ("resource", "color_space"))
TEMPLATE_LAYER = deepcopy(LAYER_V2)
TEMPLATE_LAYER["properties"]["channels"]["properties"] = {k: REF for k in TEMPLATE_LAYER["properties"]["channels"]["properties"]}
TEMPLATE_LAYER["properties"]["mask"] = {"anyOf": [REF, {"type": "null"}], "default": None}
effect = deepcopy(LAYER_V2["properties"]["effect"]["anyOf"][0])
del effect["properties"]["group"]
del effect["properties"]["expected_sha256"]
effect["properties"]["group_resource"] = RESOURCE_ID
effect["required"] = ["group_resource", "input_socket", "output_socket"]
TEMPLATE_LAYER["properties"]["effect"] = {"anyOf": [effect, {"type": "null"}], "default": None}
TEMPLATE_SCHEMA = obj({
    "template_schema_version": {"const": "1.0"}, "id": ID, "name": NAME,
    "version": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"},
    "shader": {"const": "pbr_layers_v2"},
    "layers": {"type": "array", "items": TEMPLATE_LAYER, "minItems": 1, "maxItems": 8},
    "resource_slots": {"type": "object"}, "group_slots": {"type": "object"},
}, ("template_schema_version", "id", "name", "version", "shader", "layers", "resource_slots", "group_slots"))
# Dynamic maps are checked explicitly; the shared validator accepts only declared object fields.
TEMPLATE_SCHEMA["properties"]["resource_slots"] = {}
TEMPLATE_SCHEMA["properties"]["group_slots"] = {}

def _slots(layers):
    resources, groups = {}, {}
    for layer in layers:
        for channel, ref in layer["channels"].items():
            color = "sRGB" if channel in ("base_color", "emission") else "Non-Color"
            if ref["color_space"] != color: _fail("template.channels", "incorrect color space")
            key = ref["resource"]
            slot = {"color_space": color}
            if key in resources and resources[key] != slot: _fail("resource_slots", "conflicting resource use")
            resources[key] = slot
        if layer["mask"]:
            ref = layer["mask"]
            if ref["color_space"] != "Non-Color": _fail("template.mask", "requires Non-Color")
            key = ref["resource"]
            slot = {"color_space": "Non-Color"}
            if key in resources and resources[key] != slot: _fail("resource_slots", "conflicting mask use")
            resources[key] = slot
        if layer["effect"]:
            fx = layer["effect"]
            names = [p["socket"] for p in fx["parameters"]]
            if len(names) != len(set(names)): _fail("effect", "duplicate parameter socket")
            key = fx["group_resource"]
            slot = {"input_socket": fx["input_socket"], "output_socket": fx["output_socket"]}
            if key in groups and groups[key] != slot: _fail("group_slots", "conflicting group interface")
            groups[key] = slot
    return resources, groups

def normalize_template(doc):
    result = _validate(deepcopy(doc), TEMPLATE_SCHEMA, "template")
    ids = [l["id"] for l in result["layers"]]
    if len(ids) != len(set(ids)): _fail("layers", "duplicate layer ID")
    base = result["layers"][0]
    if not base["enabled"] or base["opacity"] != 1 or base["mask"] is not None: _fail("layers", "invalid base layer")
    resources, groups = _slots(result["layers"])
    for name, expected in (("resource_slots", resources), ("group_slots", groups)):
        if result[name] != expected: _fail(name, "declarations must exactly match layer references")
    return result

def template_from_manifest(manifest, template_id, name, version):
    spec = normalize_manifest(manifest)
    layers = deepcopy(spec["layers"])
    for layer in layers:
        for channel, image in list(layer["channels"].items()):
            layer["channels"][channel] = {"resource": layer["id"] + "_" + channel, "color_space": image["color_space"]}
        if layer["mask"]:
            layer["mask"] = {"resource": layer["id"] + "_mask", "color_space": "Non-Color"}
        if layer.get("effect"):
            fx = layer["effect"]
            fx.pop("group"); fx.pop("expected_sha256")
            fx["group_resource"] = layer["id"] + "_effect"
    # A v1 material has no modulation nodes: neutral v2 adjustments retain its appearance.
    layers = [_validate(l, TEMPLATE_LAYER, "layers") for l in layers]
    resources, groups = _slots(layers)
    return normalize_template({"template_schema_version": "1.0", "id": template_id,
        "name": name, "version": version, "shader": "pbr_layers_v2", "layers": layers,
        "resource_slots": resources, "group_slots": groups})

def instantiate_template(template, bindings):
    doc = normalize_template(template)
    if not isinstance(bindings, dict) or set(bindings) != {"context", "target", "material", "preview", "resources", "groups"}:
        _fail("bindings", "requires exact context/target/material/preview/resources/groups fields")
    if not isinstance(bindings["resources"], dict) or set(bindings["resources"]) != set(doc["resource_slots"]):
        _fail("bindings.resources", "missing or extra resource bindings")
    if not isinstance(bindings["groups"], dict) or set(bindings["groups"]) != set(doc["group_slots"]):
        _fail("bindings.groups", "missing or extra group bindings")
    material = _validate(bindings["material"], obj({"id": ID, "name": NAME}, ("id", "name")), "bindings.material")
    layers = deepcopy(doc["layers"])
    def image(ref):
        value = _validate(deepcopy(bindings["resources"][ref["resource"]]), IMAGE, "bindings.image")
        if value["color_space"] != ref["color_space"]: _fail("bindings.image", "color space mismatch")
        return value
    for layer in layers:
        layer["channels"] = {k: image(v) for k, v in layer["channels"].items()}
        if layer["mask"]: layer["mask"] = image(layer["mask"])
        if layer["effect"]:
            fx = layer["effect"]
            group = _validate(bindings["groups"][fx.pop("group_resource")], obj({"group": NAME, "expected_sha256": SHA}, ("group", "expected_sha256")), "bindings.group")
            fx.update(group)
    return normalize_manifest({"schema_version": "1.1", "context": bindings["context"],
        "target": bindings["target"], "material": {**material, "template": "pbr_layers_v2"},
        "layers": layers, "preview": bindings["preview"]})

def write_template(path, doc):
    normalized = normalize_template(doc)
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(normalized, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return normalized

def read_template(path):
    with Path(path).open(encoding="utf-8") as handle:
        return normalize_template(json.load(handle))

def builtin_template(name):
    if name not in ("single_layer", "base_overlay"): _fail("builtin", "unknown template")
    layers = [{"id": "base", "name": "Base"}]
    if name == "base_overlay": layers.append({"id": "overlay", "name": "Overlay", "opacity": 0.5})
    layers = [_validate(l, TEMPLATE_LAYER, "layers") for l in layers]
    return normalize_template({"template_schema_version": "1.0", "id": name,
        "name": name, "version": "1.0.0", "shader": "pbr_layers_v2", "layers": layers,
        "resource_slots": {}, "group_slots": {}})

def builtin_templates():
    return [builtin_template(name) for name in ("single_layer", "base_overlay")]
