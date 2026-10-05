# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict, host-compatible contract shared by the Blender add-on and CLI."""
from copy import deepcopy
import math
from pathlib import Path
import re

class WorkflowError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def obj(properties, required=(), **extra):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False, **extra}
def num(low, high, default=None):
    out = {"type": "number", "minimum": low, "maximum": high}
    if default is not None: out["default"] = default
    return out
def vec(length, low, high, default):
    return {"type": "array", "items": num(low, high), "minItems": length,
            "maxItems": length, "default": default}
NAME = {"type": "string", "minLength": 1, "maxLength": 63, "pattern": r"^[^\x00-\x1f\x7f]+$"}
ID = {"type": "string", "minLength": 1, "maxLength": 48, "pattern": r"^[A-Za-z][A-Za-z0-9_-]*$"}
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
FILE = obj({"file": {"type": "string", "minLength": 1}, "expected_sha256": SHA},
           ("file", "expected_sha256"))
IMAGE = obj({**FILE["properties"], "color_space": {"enum": ["sRGB", "Non-Color"]}},
            ("file", "expected_sha256", "color_space"))
VALUES = obj({
    "base_color": vec(4, 0, 1, [0.5, 0.5, 0.5, 1]),
    "roughness": num(0, 1, 0.5), "metallic": num(0, 1, 0),
    "normal_strength": num(0, 10, 1), "height_distance": num(0, 1, 0.001),
    "alpha": num(0, 1, 1), "emission_color": vec(4, 0, 1, [0, 0, 0, 1]),
    "emission_strength": num(0, 100, 0),
}, default={})
MAPPING = obj({
    "scale": vec(3, 0.00001, 10000, [1, 1, 1]),
    "rotation": vec(3, -1000, 1000, [0, 0, 0]),
    "translation": vec(3, -10000, 10000, [0, 0, 0]),
}, default={})
CHANNELS = ("base_color", "roughness", "metallic", "normal", "height", "alpha", "emission")
LAYER = obj({
    "id": ID, "name": NAME, "enabled": {"type": "boolean", "default": True},
    "opacity": num(0, 1, 1),
    "mask": {"anyOf": [IMAGE, {"type": "null"}], "default": None},
    "channels": obj({key: IMAGE for key in CHANNELS}, default={}),
    "values": VALUES, "mapping": MAPPING,
}, ("id", "name"))
ADJUSTMENTS = obj({"color_tint": vec(4, 0, 1, [1, 1, 1, 1]),
                   "roughness_scale": num(0, 10, 1), "roughness_bias": num(-1, 1, 0)}, default={})
EFFECT = obj({"group": NAME, "expected_sha256": SHA,
              "input_socket": NAME, "output_socket": NAME,
              "parameters": {"type": "array", "maxItems": 64, "default": [], "items": obj({
                  "socket": NAME, "value": {"anyOf": [num(-10000, 10000),
                      vec(3, -10000, 10000, [0, 0, 0]), vec(4, -10000, 10000, [0, 0, 0, 0])]}
              }, ("socket", "value"))}}, ("group", "expected_sha256", "input_socket", "output_socket"))
LAYER_V2 = deepcopy(LAYER)
# Blender 5.2.1 Principled defaults, verified by the runtime RNA probe.
# Keep v1 closed and unchanged; these are constant v2 shader controls.
LAYER_V2["properties"]["values"]["properties"].update({
    "ior": num(1, 4, 1.5), "coat_weight": num(0, 1, 0),
    "coat_roughness": num(0, 1, 0.03), "coat_ior": num(1, 4, 1.5),
})
LAYER_V2["properties"].update({"adjustments": ADJUSTMENTS,
    "effect": {"anyOf": [EFFECT, {"type": "null"}], "default": None}})
VIEW = obj({
    "id": ID, "azimuth": num(-360, 360, 45), "elevation": num(-80, 89, 15),
    "lighting": {"enum": ["soft", "raking"], "default": "soft"},
    "focus": vec(3, 0, 1, [0.5, 0.5, 0.5]), "zoom": num(0.25, 10, 1),
}, ("id",))
PREVIEW = obj({
    "engine": {"enum": ["CYCLES", "BLENDER_EEVEE"], "default": "CYCLES"},
    "device": {"anyOf": [obj({"backend": {"enum": ["CPU", "GRAPHICS"]}}, ("backend",)),
        obj({"backend": {"enum": ["CUDA", "OPTIX"]}, "id": {"type": "string", "minLength": 1}}, ("backend", "id"))]},
    "width": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 384},
    "height": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 384},
    "samples": {"type": "integer", "minimum": 1, "maximum": 256, "default": 16},
    "denoise": {"type": "boolean"},
    "color": obj({"view": {"enum": ["AgX", "Standard"], "default": "AgX"},
                  "exposure": num(-10, 10, 0), "gamma": num(0.25, 4, 1)}, default={}),
    "margin": num(1.01, 3, 1.15),
    "views": {"type": "array", "items": VIEW, "minItems": 1, "maxItems": 12},
}, ("device", "views"))
# Conditions stay separate from views so batch contracts can extend view objects.
PREVIEW["allOf"] = [{"if": {"required": ["engine"], "properties": {"engine": {"const": "BLENDER_EEVEE"}}},
    "then": {"properties": {"device": {"properties": {"backend": {"const": "GRAPHICS"}}}, "denoise": {"const": False}}},
    "else": {"properties": {"device": {"properties": {"backend": {"enum": ["CPU", "CUDA", "OPTIX"]}}}}}}]
MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Material workflow M1 manifest 1.0",
    **obj({
        "schema_version": {"const": "1.0"},
        "context": obj({"scene": NAME, "view_layer": NAME,
                        "frame": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 1}},
                       ("scene", "view_layer")),
        "target": obj({"object": NAME, "material_slot": {"type": "integer", "minimum": 0, "maximum": 63, "default": 0},
                       "uv_layer": NAME}, ("object", "uv_layer")),
        "material": obj({"id": ID, "name": NAME,
                         "template": {"const": "pbr_layers_v1", "default": "pbr_layers_v1"}}, ("id", "name")),
        "layers": {"type": "array", "items": LAYER, "minItems": 1, "maxItems": 8},
        "preview": PREVIEW,
    }, ("schema_version", "context", "target", "material", "layers", "preview"))
}
MANIFEST_V1_SCHEMA = deepcopy(MANIFEST_SCHEMA)
MANIFEST_V2_SCHEMA = deepcopy(MANIFEST_SCHEMA)
MANIFEST_V2_SCHEMA["properties"]["schema_version"] = {"const": "1.1"}
MANIFEST_V2_SCHEMA["properties"]["material"]["properties"]["template"] = {"const": "pbr_layers_v2", "default": "pbr_layers_v2"}
MANIFEST_V2_SCHEMA["properties"]["layers"]["items"] = LAYER_V2
# Preserve properties for schema exporters while the alternatives enforce version pairing.
MANIFEST_SCHEMA["title"] = "Material workflow manifest 1.0/1.1"
MANIFEST_SCHEMA["properties"]["schema_version"] = {"enum": ["1.0", "1.1"]}
MANIFEST_SCHEMA["properties"]["material"]["properties"]["template"] = {"enum": ["pbr_layers_v1", "pbr_layers_v2"]}
MANIFEST_SCHEMA["properties"]["layers"]["items"] = LAYER_V2
MANIFEST_SCHEMA["anyOf"] = [MANIFEST_V1_SCHEMA, MANIFEST_V2_SCHEMA]

def _fail(path, message):
    raise WorkflowError("INVALID_REQUEST", path + ": " + message)

def _constraints(value, schema, path):
    """Validate conditional schema subsets without filling defaults or closing objects."""
    if 'const' in schema and value != schema['const']: _fail(path, 'unsupported engine/device option')
    if 'enum' in schema and value not in schema['enum']: _fail(path, 'unsupported engine/device option')
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value: _fail(path, 'missing '+key)
        for key, child in schema.get('properties', {}).items():
            if key in value: _constraints(value[key], child, path+'.'+key)
    if 'if' in schema:
        try: _constraints(value, schema['if'], path); matched=True
        except WorkflowError: matched=False
        _constraints(value, schema.get('then' if matched else 'else', {}), path)
    for child in schema.get('allOf', []): _constraints(value, child, path)

def _validate(value, schema, path):
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try: return _validate(deepcopy(value), choice, path)
            except WorkflowError: pass
        _fail(path, "does not match any permitted type")
    if "const" in schema and value != schema["const"]: _fail(path, "unsupported constant")
    if "enum" in schema and value not in schema["enum"]: _fail(path, "unsupported value")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict): _fail(path, "expected object")
        props = schema["properties"]
        if set(value) - set(props): _fail(path, "unknown fields " + str(sorted(set(value) - set(props))))
        result = {}
        for key, spec in props.items():
            if key in value: result[key] = _validate(value[key], spec, path + "." + key)
            elif "default" in spec: result[key] = _validate(deepcopy(spec["default"]), spec, path + "." + key)
            elif key in schema.get("required", []): _fail(path, "missing " + key)
        _constraints(result, schema, path)
        if 'engine' in props and 'denoise' in props and 'device' in props:
            result.setdefault('denoise', result['engine']=='CYCLES')
        return result
    if kind == "array":
        if not isinstance(value, list): _fail(path, "expected array")
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10**9): _fail(path, "invalid length")
        return [_validate(x, schema["items"], path + "[" + str(i) + "]") for i, x in enumerate(value)]
    if kind == "string":
        if not isinstance(value, str): _fail(path, "expected string")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10**9): _fail(path, "invalid string length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value): _fail(path, "invalid string pattern")
    elif kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): _fail(path, "expected finite number")
        if kind == "integer" and not isinstance(value, int): _fail(path, "expected integer")
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf): _fail(path, "number out of range")
    elif kind == "boolean" and not isinstance(value, bool): _fail(path, "expected boolean")
    elif kind == "null" and value is not None: _fail(path, "expected null")
    return value

def _absolute(path):
    p = Path(path)
    if not p.is_absolute(): _fail("file", "absolute input path required")
    if any(token in path for token in ("<UDIM>", "<UVTILE>")): _fail("file", "UDIM is outside M1")
    return str(p.resolve())

def image_documents(manifest):
    result = []
    for layer in manifest["layers"]:
        result.extend(layer["channels"].values())
        if layer["mask"] is not None: result.append(layer["mask"])
    return result

def normalize_manifest(value):
    spec = _validate(deepcopy(value), MANIFEST_SCHEMA, "manifest")
    for collection in (spec["layers"], spec["preview"]["views"]):
        ids = [item["id"] for item in collection]
        if len(ids) != len(set(ids)): _fail("manifest", "duplicate stable IDs")
    view_ids = [view['id'].casefold() for view in spec['preview']['views']]
    if len(view_ids) != len(set(view_ids)):
        _fail('preview.views', 'view IDs must be unique ignoring case on Windows')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    if reserved.intersection(view_ids):
        _fail('preview.views', 'Windows reserved directory name')
    base = spec["layers"][0]
    if not base["enabled"] or base["opacity"] != 1 or base["mask"] is not None:
        _fail("manifest.layers[0]", "base layer must be enabled, opaque and unmasked")
    known = {}
    for layer in spec["layers"]:
        if layer.get("effect"):
            names = [item["socket"] for item in layer["effect"]["parameters"]]
            if len(names) != len(set(names)): _fail("effect.parameters", "duplicate socket names")
        for channel, image in layer["channels"].items():
            required = "sRGB" if channel in ("base_color", "emission") else "Non-Color"
            if image["color_space"] != required: _fail("channels." + channel, "requires " + required)
        if layer["mask"] and layer["mask"]["color_space"] != "Non-Color": _fail("mask", "requires Non-Color")
    for image in image_documents(spec):
        image["file"] = _absolute(image["file"])
        path, sha = image["file"], image["expected_sha256"]
        if path in known and known[path] != sha: _fail("file", "one path declares conflicting hashes")
        known[path] = sha
    device = spec["preview"]["device"]
    if device["backend"] in ("CUDA", "OPTIX") and not device.get("id"): _fail("device", "GPU requires exact detected device id")
    if device["backend"] in ("CPU", "GRAPHICS") and "id" in device: _fail("device", "CPU/GRAPHICS must not declare a GPU id")
    return spec

def normalize_params(value):
    schema = obj({"file": {"type": "string", "minLength": 1}, "expected_sha256": SHA,
                  "manifest": MANIFEST_SCHEMA, "resources": {"type": "array", "items": FILE, "maxItems": 256}},
                 ("file", "expected_sha256", "manifest", "resources"))
    result = _validate(deepcopy(value), schema, "params")
    result["file"] = _absolute(result["file"])
    if Path(result["file"]).suffix.lower() != ".blend": _fail("file", "expected .blend source")
    result["manifest"] = normalize_manifest(result["manifest"])
    resources = {}
    for item in result["resources"]:
        item["file"] = _absolute(item["file"])
        if item["file"] in resources: _fail("resources", "duplicate resource path")
        resources[item["file"]] = item["expected_sha256"]
    for item in image_documents(result["manifest"]):
        if resources.get(item["file"]) != item["expected_sha256"]:
            _fail("resources", "every layer image needs the same declared file/hash")
    return result
