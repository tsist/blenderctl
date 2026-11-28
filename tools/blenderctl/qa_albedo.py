"""Sample non-transparent dielectric Base Color in sRGB byte units."""

from pathlib import Path
from tempfile import TemporaryDirectory

import imbuf
import numpy as np

from qa_udim import tiled_files


def _linear_to_srgb(values):
    values = np.maximum(values, 0.0)
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1.0 / 2.4) - 0.055)


def _summary(pixels, *, float_linear):
    if pixels.ndim != 2 or pixels.shape[1] < 3:
        return {"status": "unmeasured", "reason": "no RGB buffer"}
    if pixels.shape[1] == 4:
        pixels = pixels[pixels[:, 3] > 0]
    values = pixels[:, :3]
    values = values[np.isfinite(values).all(axis=1)]
    if not len(values):
        return {"status": "unmeasured", "reason": "no finite non-transparent pixels"}
    if float_linear:
        values = _linear_to_srgb(values)
    if values.dtype.kind == "f":
        values *= 255
    # Ignore the 1% tails at each end while retaining the actual observed values.
    intensities = np.sort(values.max(axis=1))
    discard = int(len(intensities) * 0.01)
    low, high = float(intensities[discard]), float(intensities[-(discard + 1)])
    return {
        "status": "pass" if low >= 29.5 and high <= 240.5 else "review",
        "sample_count": len(values), "trim_fraction": 0.02,
        "srgb_value_min": round(low, 3), "srgb_value_max": round(high, 3),
        "rule": "dielectric 30..240 sRGB",
    }


def _grid(array):
    """Sample pixel centers in the same normalized UV grid for color and ORM."""
    height, width = array.shape[:2]
    centers = (np.arange(64) + 0.5) / 64
    return array[np.ix_((centers * height).astype(int), (centers * width).astype(int))].reshape(4096, -1).copy()


def _sample_file(path):
    buffer = imbuf.load(str(path))
    try:
        with buffer.with_buffer() as view:
            return _grid(np.asarray(view)), buffer.buffer_type == "FLOAT"
    finally:
        buffer.free()


def _samples(image, cache):
    import bpy

    key = image.as_pointer()
    if key in cache:
        return cache[key]
    packed = {Path(entry.filepath).name: entry.packed_file for entry in image.packed_files}
    files = (list(tiled_files(image)) if image.source == "TILED" else
             [(None, Path(bpy.path.abspath(image.filepath, library=image.library)))])
    result = {}
    with TemporaryDirectory(prefix="blend-qa-albedo-") as temp:
        for tile, path in files:
            if image.source == "GENERATED":
                width, height = image.size
                if not width or not height:
                    raise ValueError("empty generated texture")
                # RNA access stays bounded even for large generated images.
                points = (np.arange(64) + .5) / 64
                offsets = ((points * height).astype(int)[:, None] * width +
                           (points * width).astype(int)[None, :]).ravel() * image.channels
                pixels = np.array([image.pixels[int(i):int(i)+image.channels] for i in offsets])
                result[tile] = pixels, True
                continue
            if image.source not in {"TILED", "FILE"}:
                raise ValueError(f"unsupported image source {image.source}")
            member = packed.get(path.name)
            if member is None and image.source == "FILE":
                member = image.packed_file
            if member is not None:
                path = Path(temp) / ("packed" + path.suffix)
                path.write_bytes(member.data)
            result[tile] = _sample_file(path)
    cache[key] = result
    return result


def _source(port):
    seen = set()
    while port.is_linked:
        output = port.links[0].from_socket
        if output.node.type != "REROUTE":
            return output
        if output.node.as_pointer() in seen:
            break
        seen.add(output.node.as_pointer())
        port = output.node.inputs[0]
    raise ValueError("unconnected or cyclic material reroute")


def _texture(port, *, scalar):
    source = _source(port)
    channel = None
    if scalar and source.node.type in {"SEPARATE_COLOR", "SEPRGB"}:
        if getattr(source.node, "mode", "RGB") != "RGB":
            raise ValueError("non-RGB component expression")
        channel = {"R": 0, "Red": 0, "G": 1, "Green": 1, "B": 2, "Blue": 2}[source.name]
        source = _source(source.node.inputs[0])
    if source.node.type != "TEX_IMAGE" or not source.node.image:
        raise ValueError(f"{port.name} uses a shader expression")
    if source.name == "Alpha" and scalar and channel is None:
        channel = 3
    elif source.name != "Color":
        raise ValueError(f"unsupported {port.name} texture output")
    return source.node, channel


def _mapping(node):
    port = node.inputs["Vector"]
    if not port.is_linked:
        return ("UV", "")
    src = _source(port)
    if src.node.type == "UVMAP":
        return ("UV", src.node.uv_map)
    if src.node.type == "TEX_COORD" and src.name == "UV":
        return ("UV", "")
    return (src.node.as_pointer(), src.identifier)


def _measure(node, cache):
    color, metallic = node.inputs["Base Color"], node.inputs["Metallic"]
    if not metallic.is_linked and metallic.default_value >= .2:
        return []
    color_node, _ = _texture(color, scalar=False) if color.is_linked else (None, None)
    mask, component = _texture(metallic, scalar=True) if metallic.is_linked else (None, None)
    if color_node and color_node.image.colorspace_settings.name not in {"sRGB", "Linear Rec.709"}:
        raise ValueError(f"source color space {color_node.image.colorspace_settings.name}")
    if color_node and mask and (_mapping(color_node) != _mapping(mask) or color_node.extension != mask.extension):
        raise ValueError("Base Color and Metallic use different UV mappings")
    colors = _samples(color_node.image, cache) if color_node else {None: (np.array([color.default_value[:]]), True)}
    masks = _samples(mask.image, cache) if mask else {}
    results = []
    tiles = (set(colors) | set(masks)) - {None}
    for tile in sorted(tiles) or [None]:
        color_entry = colors.get(tile, colors.get(None))
        if color_entry is None:
            raise ValueError(f"missing Base Color tile {tile}")
        pixels, linear = color_entry
        if mask:
            mask_entry = masks.get(tile, masks.get(None))
            if mask_entry is None:
                raise ValueError(f"missing Metallic tile {tile}")
            raw, _ = mask_entry
            values = raw.astype(float) / (255 if raw.dtype.kind in "ui" else 1)
            if mask.image.colorspace_settings.name == "sRGB" and raw.dtype.kind in "ui":
                rgb = values[:, :3]
                values[:, :3] = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
            metal = (values[:, component] if component is not None else
                     values[:, :3] @ np.array([.2126, .7152, .0722]))
            dielectric = np.isfinite(metal) & (metal < .2)
            if len(pixels) == 1:
                pixels = np.broadcast_to(pixels, (len(metal), pixels.shape[1]))
            pixels = pixels[dielectric]
            if not len(pixels):
                results.append({"tile": tile, "status": "not_applicable", "reason": "no dielectric pixels"})
                continue
        row = _summary(pixels, float_linear=linear)
        results.append({**row, "tile": tile,
                        **({"image": color_node.image.name} if color_node else {}),
                        **({"metallic_mask": mask.image.name, "metallic_channel": component} if mask else {})})
    return results


def sample_material(material, cache):
    if not material.use_nodes or not material.node_tree:
        return []
    results = []
    for node in material.node_tree.nodes:
        if node.type != "BSDF_PRINCIPLED":
            continue
        try:
            rows = _measure(node, cache)
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            rows = [{"status": "unmeasured", "reason": str(exc)}]
        results.extend({"material": material.name, "principled": node.name, **row} for row in rows)
    return results
