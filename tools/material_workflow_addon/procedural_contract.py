"""Strict contract for the procedural PBR material manifest (schema 2.0).

This module is deliberately host-free.  It depends only on the standard
library and :mod:`contract`, so it can be validated from the CLI and
from Blender without importing ``bpy``.
"""

from collections import deque
from copy import deepcopy
from pathlib import Path
import math
import struct

from . import contract as _legacy


# Re-export the public error type and schema helpers used by callers/tests.
WorkflowError = _legacy.WorkflowError
obj = _legacy.obj
num = _legacy.num
vec = _legacy.vec
ID = _legacy.ID
NAME = _legacy.NAME
IMAGE = _legacy.IMAGE
PREVIEW = deepcopy(_legacy.PREVIEW)
_validate = _legacy._validate
_absolute = _legacy._absolute
_fail = _legacy._fail


SCHEMA_VERSION = "2.0"
MATERIAL_TEMPLATE = "procedural_pbr_v1"

VALUE = "VALUE"
COLOR = "COLOR"
VECTOR = "VECTOR"

MATERIAL_CHANNELS = (
    "base_color",
    "roughness",
    "metallic",
    "height",
    "alpha",
    "transmission",
    "ior",
    "coat_weight",
    "coat_roughness",
)
CHANNEL_OUTPUT_TYPES = {
    "base_color": COLOR,
    "roughness": VALUE,
    "metallic": VALUE,
    "height": VALUE,
    "alpha": VALUE,
    "transmission": VALUE,
    "ior": VALUE,
    "coat_weight": VALUE,
    "coat_roughness": VALUE,
}

DEFAULT_EXPORT_CHANNELS = ("base_color", "roughness", "metallic", "normal")
EXPORT_CHANNELS = (
    "base_color",
    "roughness",
    "metallic",
    "normal",
    "height",
    "alpha",
    "transmission",
    "ior",
    "coat_weight",
    "coat_roughness",
)

# These signals rely on geometry-dependent Cycles facilities.  They still have
# a scalar VALUE contract here; execution must provide the Cycles implementation.
CYCLES_GEOMETRIC_SIGNAL_TYPES = frozenset({"edge", "cavity"})

MATH_OPERATIONS = (
    "MULTIPLY",
    "ADD",
    "SUBTRACT",
    "MINIMUM",
    "MAXIMUM",
    "POWER",
    "GREATER_THAN",
    "LESS_THAN",
)


# Context, target and preview intentionally come from the frozen legacy schema
# rather than being restated here.  Deep copies keep the two schemas independent.
CONTEXT_SCHEMA = deepcopy(_legacy.MANIFEST_SCHEMA["properties"]["context"])
TARGET_SCHEMA = deepcopy(_legacy.MANIFEST_SCHEMA["properties"]["target"])
PREVIEW_SCHEMA = deepcopy(_legacy.MANIFEST_SCHEMA["properties"]["preview"])


def _finite_vec(length, default):
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": length,
        "maxItems": length,
        "default": list(default),
    }


RGBA = {
    "type": "array",
    "items": num(0, 1),
    "minItems": 4,
    "maxItems": 4,
}

COORDINATE = obj(
    {
        "space": {"enum": ["OBJECT", "UV"], "default": "OBJECT"},
        "scale": {
            "type": "array",
            "items": {"type": "number", "exclusiveMinimum": 0},
            "minItems": 3,
            "maxItems": 3,
            "default": [1, 1, 1],
        },
        "rotation": _finite_vec(3, [0, 0, 0]),
        "translation": _finite_vec(3, [0, 0, 0]),
    },
    default={},
)


def _noise_parameter_properties():
    return {
        "scale": num(0.0001, 10000, 5),
        "detail": num(0, 15, 2),
        "roughness": num(0, 1, 0.5),
        "lacunarity": num(0, 1000, 2),
        "distortion": num(0, 100, 0),
        "seed": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
        "coordinate": deepcopy(COORDINATE),
    }


CONSTANT_PARAMETERS = obj(
    {"value": {"anyOf": [{"type": "number"}, deepcopy(RGBA)]}},
    ("value",),
)
IMAGE_PARAMETERS = obj(
    {
        "image": deepcopy(IMAGE),
        "output": {"enum": [COLOR, VALUE], "default": COLOR},
    },
    ("image",),
)
NOISE_PARAMETERS = obj(_noise_parameter_properties())
COORDINATE_PARAMETERS = obj({"coordinate": deepcopy(COORDINATE)})
WARP_PARAMETERS = obj({
    **{key: value for key, value in _noise_parameter_properties().items()
       if key not in ("distortion", "coordinate")},
    "sample_scale": deepcopy(COORDINATE["properties"]["scale"]),
    "strength": _finite_vec(3, [0.1, 0.1, 0.1]),
})
VORONOI_PARAMETERS = obj({
    "scale": num(0.0001, 10000, 5),
    "randomness": num(0, 1, 1),
    "feature": {"enum": ["F1", "DISTANCE_TO_EDGE"], "default": "F1"},
    "coordinate": deepcopy(COORDINATE),
})
WOOD_PARAMETERS = obj(
    {
        **{key: value for key, value in _noise_parameter_properties().items() if key != "lacunarity"},
        "direction": {"enum": ["X", "Y", "Z"], "default": "Z"},
        "rings": {"type": "boolean", "default": False},
        "detail_scale": num(0, 10000, 1),
        "profile": {"enum": ["SIN", "SAW", "TRI"], "default": "SIN"},
    }
)
EDGE_PARAMETERS = obj(
    {
        "width": num(0.00001, 1, 0.003),
        "samples": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
        "gain": num(0.1, 100, 10),
    }
)
CAVITY_PARAMETERS = obj(
    {
        "distance": num(0.00001, 100, 0.1),
        "samples": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
        "only_local": {"type": "boolean", "default": True},
    }
)
HEIGHT_BAND_PARAMETERS = obj(
    {
        "axis": {"enum": ["X", "Y", "Z"], "default": "Z"},
        "low": num(-10000, 10000, 0),
        "high": num(-10000, 10000, 1),
        "coordinate": deepcopy(COORDINATE),
    }
)
NORMAL_DIRECTION_PARAMETERS = obj(
    {
        "axis": {"enum": ["X", "Y", "Z"], "default": "Z"},
        "invert": {"type": "boolean", "default": False},
    }
)
MATH_PARAMETERS = obj(
    {
        "operation": {"enum": list(MATH_OPERATIONS)},
        "clamp": {"type": "boolean", "default": True},
    },
    ("operation",),
)
RAMP_STOP = obj(
    {
        "position": num(0, 1),
        "color": deepcopy(RGBA),
    },
    ("position", "color"),
)
RAMP_PARAMETERS = obj(
    {
        "stops": {
            "type": "array",
            "items": RAMP_STOP,
            "minItems": 2,
            "maxItems": 8,
        }
    },
    ("stops",),
)
MIX_PARAMETERS = obj({})
REMAP_PARAMETERS = obj({
    "from_min": {"type": "number", "default": 0},
    "from_max": {"type": "number", "default": 1},
    "to_min": {"type": "number", "default": 0},
    "to_max": {"type": "number", "default": 1},
    "clamp": {"type": "boolean", "default": True},
    "interpolation": {"enum": ["LINEAR", "SMOOTHSTEP", "SMOOTHERSTEP"], "default": "LINEAR"},
})

EMPTY_INPUTS = obj({})
OPTIONAL_VECTOR_INPUTS = obj({"vector": deepcopy(ID)})
VECTOR_INPUTS = obj({"vector": deepcopy(ID)}, ("vector",))
MATH_INPUTS = obj({"a": deepcopy(ID), "b": deepcopy(ID)}, ("a", "b"))
RAMP_INPUTS = obj({"value": deepcopy(ID)}, ("value",))
MIX_INPUTS = obj(
    {"factor": deepcopy(ID), "a": deepcopy(ID), "b": deepcopy(ID)},
    ("factor", "a", "b"),
)


def _signal_schema(signal_type, parameters, inputs):
    return obj(
        {
            "id": deepcopy(ID),
            "type": {"const": signal_type},
            "parameters": parameters,
            "inputs": inputs,
        },
        ("id", "type", "parameters", "inputs"),
    )


SIGNAL_SCHEMA = {
    "anyOf": [
        _signal_schema("constant", CONSTANT_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("image", IMAGE_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("coordinate", COORDINATE_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("warp", WARP_PARAMETERS, VECTOR_INPUTS),
        _signal_schema("noise", NOISE_PARAMETERS, OPTIONAL_VECTOR_INPUTS),
        _signal_schema("wood", WOOD_PARAMETERS, OPTIONAL_VECTOR_INPUTS),
        _signal_schema("voronoi", VORONOI_PARAMETERS, OPTIONAL_VECTOR_INPUTS),
        _signal_schema("edge", EDGE_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("cavity", CAVITY_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("height_band", HEIGHT_BAND_PARAMETERS, OPTIONAL_VECTOR_INPUTS),
        _signal_schema("normal_direction", NORMAL_DIRECTION_PARAMETERS, EMPTY_INPUTS),
        _signal_schema("math", MATH_PARAMETERS, MATH_INPUTS),
        _signal_schema("ramp", RAMP_PARAMETERS, RAMP_INPUTS),
        _signal_schema("remap", REMAP_PARAMETERS, RAMP_INPUTS),
        _signal_schema("mix", MIX_PARAMETERS, MIX_INPUTS),
    ]
}

LAYER_VALUES_SCHEMA = obj(
    {
        "base_color": vec(4, 0, 1, [0.5, 0.5, 0.5, 1]),
        "roughness": num(0, 1, 0.5),
        "metallic": num(0, 1, 0),
        "height": num(-1, 1, 0),
        "alpha": num(0, 1, 1),
        "transmission": num(0, 1, 0),
        "ior": num(1, 4, 1.5),
        "coat_weight": num(0, 1, 0),
        "coat_roughness": num(0, 1, 0.03),
    },
    default={},
)
LAYER_CHANNELS_SCHEMA = obj(
    {channel: deepcopy(ID) for channel in MATERIAL_CHANNELS},
    default={},
)
LAYER_SCHEMA = obj(
    {
        "id": deepcopy(ID),
        "name": deepcopy(NAME),
        "enabled": {"type": "boolean", "default": True},
        "opacity": num(0, 1, 1),
        "mask": {"anyOf": [deepcopy(ID), {"type": "null"}], "default": None},
        "channels": deepcopy(LAYER_CHANNELS_SCHEMA),
        "values": deepcopy(LAYER_VALUES_SCHEMA),
    },
    ("id", "name"),
)


MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Material workflow procedural manifest 2.0",
    **obj(
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "context": deepcopy(CONTEXT_SCHEMA),
            "target": deepcopy(TARGET_SCHEMA),
            "material": obj(
                {
                    "id": deepcopy(ID),
                    "name": deepcopy(NAME),
                    "template": {
                        "const": MATERIAL_TEMPLATE,
                        "default": MATERIAL_TEMPLATE,
                    },
                },
                ("id", "name"),
            ),
            "signals": {
                "type": "array",
                "items": SIGNAL_SCHEMA,
                "minItems": 1,
                "maxItems": 96,
            },
            "layers": {
                "type": "array",
                "items": LAYER_SCHEMA,
                "minItems": 1,
                "maxItems": 8,
            },
            "preview": deepcopy(PREVIEW_SCHEMA),
            "height_distance": num(0, 1, 0.001),
            # Absence is intentional: legacy recipes keep the identical
            # normalized document and automatic (unlinked) Thickness behavior.
            "surface": obj({
                "thickness_mode": {"enum": ["SPHERE", "SLAB"]},
                "thickness": {**num(0, 1000), "description":
                    "Optional finite float32 object-space Thickness scalar; connected zero disables the approximation. Omit for AUTO."},
            }, ("thickness_mode",)),
            "mesh_map_size": {
                "type": "integer",
                "minimum": 64,
                "maximum": 2048,
                "default": 512,
            },
        },
        (
            "schema_version",
            "context",
            "target",
            "material",
            "signals",
            "layers",
            "preview",
        ),
    ),
}


FILE = deepcopy(_legacy.FILE)
EXPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "PBR texture export settings",
    **obj(
        {
            "channels": {
                "type": "array",
                "items": {"enum": list(EXPORT_CHANNELS)},
                "minItems": 1,
                "uniqueItems": True,
                "default": list(DEFAULT_EXPORT_CHANNELS),
            },
            "size": {
                "type": "integer",
                "minimum": 64,
                "maximum": 2048,
                "default": 2048,
            },
            "margin": {
                "type": "integer",
                "minimum": 0,
                "maximum": 128,
                "default": 16,
            },
            "samples": {
                "type": "integer",
                "minimum": 1,
                "maximum": 256,
                "default": 16,
            },
            "bit_depth": {"enum": ["8", "16"], "default": "16"},
        }
    ),
}


_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_WINDOWS_INVALID = frozenset('<>:"/\\|?*')


def _check_windows_component(value, path):
    """Reject names that cannot safely be used as a Windows path component."""
    if value in (".", "..") or value[-1] in (" ", "."):
        _fail(path, "must be a safe Windows path component")
    if any(character in _WINDOWS_INVALID for character in value):
        _fail(path, "must be a safe Windows path component")
    stem = value.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED:
        _fail(path, "Windows reserved file name")


def _check_coordinate(coordinate, path):
    for index, component in enumerate(coordinate["scale"]):
        if component <= 0:
            _fail(path + ".scale[" + str(index) + "]", "must be positive")


def _restore_signal_input_order(normalized, original):
    """Preserve author key order that the schema walk necessarily canonicalizes."""
    if not isinstance(original, dict):
        return
    original_signals = original.get("signals")
    if not isinstance(original_signals, list):
        return
    for signal, raw_signal in zip(normalized["signals"], original_signals):
        if not isinstance(raw_signal, dict):
            continue
        raw_inputs = raw_signal.get("inputs")
        if not isinstance(raw_inputs, dict):
            continue
        ordered = {}
        for key in raw_inputs:
            if key in signal["inputs"]:
                ordered[key] = signal["inputs"][key]
        for key, value in signal["inputs"].items():
            ordered.setdefault(key, value)
        signal["inputs"] = ordered


def _check_identity_components(spec):
    _check_windows_component(spec["material"]["id"], "manifest.material.id")
    _check_windows_component(spec["material"]["name"], "manifest.material.name")
    for index, signal in enumerate(spec["signals"]):
        _check_windows_component(
            signal["id"], "manifest.signals[" + str(index) + "].id"
        )
    for index, layer in enumerate(spec["layers"]):
        _check_windows_component(
            layer["id"], "manifest.layers[" + str(index) + "].id"
        )
        _check_windows_component(
            layer["name"], "manifest.layers[" + str(index) + "].name"
        )


def _signal_metadata(signals):
    """Validate references and return (types-by-manifest-order, topological order)."""
    signal_by_id = {}
    for index, signal in enumerate(signals):
        signal_id = signal["id"]
        if signal_id in signal_by_id:
            _fail(
                "manifest.signals[" + str(index) + "].id",
                "duplicate stable ID",
            )
        signal_by_id[signal_id] = signal
        if signal["type"] in ("noise", "wood", "height_band", "coordinate", "voronoi"):
            _check_coordinate(
                signal["parameters"]["coordinate"],
                "manifest.signals[" + str(index) + "].parameters.coordinate",
            )
        if signal["type"] == "warp":
            for component in signal["parameters"]["sample_scale"]:
                if component <= 0:
                    _fail("manifest.signals[" + str(index) + "].parameters.sample_scale", "must be positive")
        if signal["type"] == "remap" and signal["parameters"]["from_min"] == signal["parameters"]["from_max"]:
            _fail("manifest.signals[" + str(index) + "].parameters", "from_min and from_max must differ")
        if signal["type"] == "ramp":
            stops = signal["parameters"]["stops"]
            for stop_index in range(1, len(stops)):
                if (
                    stops[stop_index]["position"]
                    <= stops[stop_index - 1]["position"]
                ):
                    _fail(
                        "manifest.signals["
                        + str(index)
                        + "].parameters.stops["
                        + str(stop_index)
                        + "].position",
                        "ramp stops must be strictly increasing and unique",
                    )

    positions = {signal["id"]: index for index, signal in enumerate(signals)}
    outgoing = {signal["id"]: [] for signal in signals}
    indegree = {signal["id"]: 0 for signal in signals}

    for index, signal in enumerate(signals):
        for slot, dependency in signal["inputs"].items():
            if dependency not in signal_by_id:
                _fail(
                    "manifest.signals[" + str(index) + "].inputs." + slot,
                    "references unknown signal ID " + repr(dependency),
                )
            indegree[signal["id"]] += 1
            outgoing[dependency].append(signal["id"])

    ready = deque(
        signal["id"] for signal in signals if indegree[signal["id"]] == 0
    )
    order = []
    inferred = {}

    def require_type(signal_index, slot, expected):
        signal = signals[signal_index]
        dependency = signal["inputs"][slot]
        actual = inferred[dependency]
        if actual != expected:
            _fail(
                "manifest.signals["
                + str(signal_index)
                + "].inputs."
                + slot,
                "requires " + expected + ", got " + actual,
            )

    while ready:
        signal_id = ready.popleft()
        order.append(signal_id)
        signal = signal_by_id[signal_id]
        index = positions[signal_id]
        signal_type = signal["type"]
        if "vector" in signal["inputs"]:
            require_type(index, "vector", VECTOR)

        if signal_type in ("coordinate", "warp"):
            output_type = VECTOR
        elif signal_type == "constant":
            output_type = (
                VALUE
                if isinstance(signal["parameters"]["value"], (int, float))
                else COLOR
            )
        elif signal_type == "image":
            output_type = signal["parameters"]["output"]
        elif signal_type == "mix":
            require_type(index, "factor", VALUE)
            a_type = inferred[signal["inputs"]["a"]]
            b_type = inferred[signal["inputs"]["b"]]
            if a_type != b_type or a_type not in (VALUE, COLOR):
                _fail(
                    "manifest.signals[" + str(index) + "].inputs",
                    "mix a and b must have the same VALUE or COLOR type",
                )
            output_type = a_type
        else:
            if signal_type == "math":
                require_type(index, "a", VALUE)
                require_type(index, "b", VALUE)
            elif signal_type in ("ramp", "remap"):
                require_type(index, "value", VALUE)
            output_type = COLOR if signal_type == "ramp" else VALUE

        inferred[signal_id] = output_type
        for dependent in outgoing[signal_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(order) != len(signals):
        blocked = {
            signal["id"] for signal in signals if indegree[signal["id"]] > 0
        }
        cycle = _find_cycle(signals, blocked)
        _fail(
            "manifest.signals",
            "dependency cycle: " + " -> ".join(repr(item) for item in cycle),
        )

    types_in_manifest_order = {
        signal["id"]: inferred[signal["id"]] for signal in signals
    }
    return types_in_manifest_order, order


def _find_cycle(signals, blocked):
    """Find one concrete cycle among the unresolved signals."""
    signal_by_id = {signal["id"]: signal for signal in signals}
    state = {}
    stack = []
    stack_positions = {}

    def visit(signal_id):
        state[signal_id] = 1
        stack_positions[signal_id] = len(stack)
        stack.append(signal_id)
        for dependency in signal_by_id[signal_id]["inputs"].values():
            if dependency not in blocked:
                continue
            if state.get(dependency) == 1:
                start = stack_positions[dependency]
                return stack[start:] + [dependency]
            if state.get(dependency, 0) == 0:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        stack_positions.pop(signal_id, None)
        state[signal_id] = 2
        return None

    for signal in signals:
        if signal["id"] in blocked and state.get(signal["id"], 0) == 0:
            cycle = visit(signal["id"])
            if cycle:
                return cycle
    return sorted(blocked)


def _image_documents_normalized(spec):
    return [
        signal["parameters"]["image"]
        for signal in spec["signals"]
        if signal["type"] == "image"
    ]


def _check_images(signals):
    known_hashes = {}
    for index, signal in enumerate(signals):
        if signal["type"] != "image":
            continue
        image = signal["parameters"]["image"]
        required_space = (
            "sRGB" if signal["parameters"]["output"] == COLOR else "Non-Color"
        )
        if image["color_space"] != required_space:
            _fail(
                "manifest.signals["
                + str(index)
                + "].parameters.image.color_space",
                "requires " + required_space,
            )
        image["file"] = _absolute(image["file"])
        path = image["file"]
        path_key = path.casefold()
        digest = image["expected_sha256"]
        if path_key in known_hashes and known_hashes[path_key] != digest:
            _fail("file", "one path declares conflicting hashes")
        known_hashes[path_key] = digest


def _check_layers(layers, signal_types):
    seen = set()
    for index, layer in enumerate(layers):
        if layer["id"] in seen:
            _fail(
                "manifest.layers[" + str(index) + "].id",
                "duplicate stable ID",
            )
        seen.add(layer["id"])

        for channel, signal_id in layer["channels"].items():
            if signal_id not in signal_types:
                _fail(
                    "manifest.layers["
                    + str(index)
                    + "].channels."
                    + channel,
                    "references unknown signal ID " + repr(signal_id),
                )
            expected = CHANNEL_OUTPUT_TYPES[channel]
            actual = signal_types[signal_id]
            if actual != expected:
                _fail(
                    "manifest.layers["
                    + str(index)
                    + "].channels."
                    + channel,
                    "requires " + expected + ", got " + actual,
                )

        mask = layer["mask"]
        if mask is not None:
            if mask not in signal_types:
                _fail(
                    "manifest.layers[" + str(index) + "].mask",
                    "references unknown signal ID " + repr(mask),
                )
            if signal_types[mask] != VALUE:
                _fail(
                    "manifest.layers[" + str(index) + "].mask",
                    "requires VALUE, got " + signal_types[mask],
                )

    base = layers[0]
    if not base["enabled"] or base["opacity"] != 1 or base["mask"] is not None:
        _fail(
            "manifest.layers[0]",
            "base layer must be enabled, opaque and unmasked",
        )


def _check_preview(preview):
    view_ids = [view["id"] for view in preview["views"]]
    if len(view_ids) != len(set(view_ids)):
        _fail("preview.views", "duplicate stable IDs")
    folded_ids = [view_id.casefold() for view_id in view_ids]
    if len(folded_ids) != len(set(folded_ids)):
        _fail("preview.views", "view IDs must be unique ignoring case on Windows")
    for index, view_id in enumerate(view_ids):
        _check_windows_component(
            view_id, "preview.views[" + str(index) + "].id"
        )

    device = preview["device"]
    if device["backend"] in ("CUDA", "OPTIX") and not device.get("id"):
        _fail("device", "GPU requires exact detected device id")
    if device["backend"] in ("CPU", "GRAPHICS") and "id" in device:
        _fail("device", "CPU/GRAPHICS must not declare a GPU id")


def _float32(value, path):
    """Reject non-finite, overflowing and nonzero-underflowing socket numbers."""
    try:
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError()
        stored = struct.unpack('f', struct.pack('f', value))[0]
        if not math.isfinite(stored) or (value != 0 and stored == 0):
            raise ValueError()
    except (ValueError, TypeError, OverflowError, struct.error):
        _fail(path, "requires a finite representable float32 socket value")
    return stored


def _socket_numbers(value, schema, path, canonical):
    """Schema-directed precision closure, limited to shader-managed fields.

    A boundary rounded one way by RNA is accepted only when it is exactly the
    float32 encoding of that declared endpoint. No blanket epsilon permits
    nearby out-of-range input or a genuinely edited adjacent float32 value.
    """
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            if ((choice.get('type') == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool))
                    or (choice.get('type') == 'array' and isinstance(value, list))):
                return _socket_numbers(value, choice, path, canonical)
        return value
    kind = schema.get('type')
    if kind == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool):
        stored = _float32(value, path)
        for bound, comparison in [('minimum', lambda a, b: a < b), ('maximum', lambda a, b: a > b)]:
            if bound not in schema:
                continue
            endpoint = schema[bound]
            if comparison(value, endpoint) and value == _float32(endpoint, path):
                value = endpoint
            if canonical and comparison(stored, endpoint):
                # Keep the declared endpoint as its canonical JSON spelling;
                # its RNA encoding is still the exact stored float32 value.
                stored = endpoint
        return stored if canonical else value
    if kind == 'object' and isinstance(value, dict):
        return {key: _socket_numbers(child, schema.get('properties', {}).get(key, {}), path + '.' + key, canonical)
                for key, child in value.items()}
    if kind == 'array' and isinstance(value, list):
        return [_socket_numbers(child, schema.get('items', {}), path + '[' + str(i) + ']', canonical)
                for i, child in enumerate(value)]
    return value


def _managed_socket_numbers(value, canonical=False):
    """Do not change preview, context, target, integer seeds or legacy schemas."""
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    by_type = {item['properties']['type']['const']: item['properties']['parameters']
               for item in SIGNAL_SCHEMA['anyOf']}
    if isinstance(result.get('signals'), list):
        for i, signal in enumerate(result['signals']):
            if isinstance(signal, dict) and isinstance(signal.get('type'), str) and 'parameters' in signal:
                signal['parameters'] = _socket_numbers(signal['parameters'], by_type.get(signal['type'], {}),
                    'manifest.signals[' + str(i) + '].parameters', canonical)
    if isinstance(result.get('layers'), list):
        for i, layer in enumerate(result['layers']):
            if not isinstance(layer, dict):
                continue
            for key in ('values', 'opacity'):
                if key in layer:
                    layer[key] = _socket_numbers(layer[key], LAYER_SCHEMA['properties'][key],
                        'manifest.layers[' + str(i) + '].' + key, canonical)
    if 'height_distance' in result:
        result['height_distance'] = _socket_numbers(result['height_distance'], MANIFEST_SCHEMA['properties']['height_distance'],
                                                   'manifest.height_distance', canonical)
    if 'surface' in result:
        result['surface'] = _socket_numbers(result['surface'], MANIFEST_SCHEMA['properties']['surface'],
                                            'manifest.surface', canonical)
    return result


def normalize_manifest(value):
    """Validate and normalize a procedural manifest without mutating *value*."""
    calibrated = _managed_socket_numbers(value)
    spec = _validate(calibrated, MANIFEST_SCHEMA, "manifest")
    spec = _managed_socket_numbers(spec, canonical=True)
    _restore_signal_input_order(spec, value)
    _check_identity_components(spec)
    signal_types, _order = _signal_metadata(spec["signals"])
    _check_images(spec["signals"])
    _check_layers(spec["layers"], signal_types)
    _check_preview(spec["preview"])
    return spec


def signal_order(manifest):
    """Return all signal IDs in a stable topological order."""
    spec = normalize_manifest(manifest)
    _types, order = _signal_metadata(spec["signals"])
    return order


def signal_types(manifest):
    """Return a mapping from each signal ID to its inferred VALUE/COLOR type."""
    spec = normalize_manifest(manifest)
    types, _order = _signal_metadata(spec["signals"])
    return types


def image_documents(manifest):
    """Return only the image documents referenced by image signals."""
    spec = normalize_manifest(manifest)
    return _image_documents_normalized(spec)


def normalize_export(value):
    """Validate standalone texture-export settings.

    Export settings are intentionally not part of a manifest.
    """
    spec = _validate(deepcopy(value), EXPORT_SCHEMA, "export")
    channels = spec["channels"]
    if len(channels) != len(set(channels)):
        _fail("export.channels", "duplicate channels")
    return spec


def normalize_params(value):
    """Validate a source .blend, its manifest, and protected image resources."""
    schema = obj(
        {
            "file": {"type": "string", "minLength": 1},
            "expected_sha256": deepcopy(_legacy.SHA),
            "manifest": MANIFEST_SCHEMA,
            "resources": {
                "type": "array",
                "items": deepcopy(FILE),
                "maxItems": 256,
            },
        },
        ("file", "expected_sha256", "manifest", "resources"),
    )
    result = _validate(deepcopy(value), schema, "params")
    result["file"] = _absolute(result["file"])
    if Path(result["file"]).suffix.lower() != ".blend":
        _fail("file", "expected .blend source")
    # Re-normalize from the caller's manifest so the author's input-key order
    # survives the outer schema walk.
    result["manifest"] = normalize_manifest(value["manifest"])

    resources = {}
    for index, item in enumerate(result["resources"]):
        item["file"] = _absolute(item["file"])
        path_key = item["file"].casefold()
        if path_key in resources:
            _fail(
                "resources[" + str(index) + "].file",
                "duplicate resource path",
            )
        resources[path_key] = item["expected_sha256"]

    for index, document in enumerate(_image_documents_normalized(result["manifest"])):
        if (
            resources.get(document["file"].casefold())
            != document["expected_sha256"]
        ):
            _fail(
                "resources",
                "image signal "
                + str(index)
                + " needs the same declared file/hash",
            )
    return result


__all__ = [
    "CHANNEL_OUTPUT_TYPES",
    "COLOR",
    "CONTEXT_SCHEMA",
    "CYCLES_GEOMETRIC_SIGNAL_TYPES",
    "DEFAULT_EXPORT_CHANNELS",
    "EXPORT_CHANNELS",
    "EXPORT_SCHEMA",
    "FILE",
    "ID",
    "IMAGE",
    "LAYER_SCHEMA",
    "LAYER_VALUES_SCHEMA",
    "MANIFEST_SCHEMA",
    "MATERIAL_CHANNELS",
    "MATERIAL_TEMPLATE",
    "NAME",
    "PREVIEW",
    "PREVIEW_SCHEMA",
    "SCHEMA_VERSION",
    "SIGNAL_SCHEMA",
    "TARGET_SCHEMA",
    "VALUE",
    "VECTOR",
    "WorkflowError",
    "image_documents",
    "normalize_export",
    "normalize_manifest",
    "normalize_params",
    "signal_order",
    "signal_types",
]
