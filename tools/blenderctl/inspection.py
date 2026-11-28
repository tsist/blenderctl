"""Versioned read-only content snapshots. The legacy transaction inspector stays frozen."""
from array import array
from collections import OrderedDict
import hashlib
import json
import math
import os
import sys
from pathlib import Path
import uuid

import bpy
from protocol import Failure, atomic_json

# Pure-content worker/module lifetime cache; module reload always creates a new
# empty OrderedDict. A file reopen may reuse equal content only after full reads.
_ATTRIBUTE_CACHE_PROTOCOL = 'legacy_canonical_json_newline_v1'
_ATTRIBUTE_CACHE_LIMIT = 512
_ATTRIBUTE_FINGERPRINT_CACHE = OrderedDict()
_ATTRIBUTE_CACHE_COUNTS = {'hits': 0, 'misses': 0, 'evictions': 0}


def _clear_attribute_fingerprint_cache():
    _ATTRIBUTE_FINGERPRINT_CACHE.clear()
    _ATTRIBUTE_CACHE_COUNTS.update(hits=0, misses=0, evictions=0)


def _attribute_fingerprint_cache_stats():
    return {**_ATTRIBUTE_CACHE_COUNTS, 'entries': len(_ATTRIBUTE_FINGERPRINT_CACHE),
            'capacity': _ATTRIBUTE_CACHE_LIMIT, 'protocol': _ATTRIBUTE_CACHE_PROTOCOL}


def _attribute_cache_key(attribute, buffers, count):
    # Complete fields were already validated/read. The key contains only small
    # metadata and full-byte digests, including the second color-space field.
    props = {p.identifier: p for p in attribute.data[0].bl_rna.properties}
    return (_ATTRIBUTE_CACHE_PROTOCOL, attribute.data_type,
            attribute.data[0].bl_rna.identifier, count,
            tuple((name, kind, width, tuple(props[name].array_dimensions),
                   buffer.typecode, buffer.itemsize, sys.byteorder, len(buffer),
                   hashlib.sha256(memoryview(buffer)).digest())
                  for name, kind, width, buffer in buffers))


def _store_attribute_fingerprint(cache_key, fingerprint):
    _ATTRIBUTE_FINGERPRINT_CACHE[cache_key] = fingerprint
    if len(_ATTRIBUTE_FINGERPRINT_CACHE) > _ATTRIBUTE_CACHE_LIMIT:
        _ATTRIBUTE_FINGERPRINT_CACHE.popitem(last=False)
        _ATTRIBUTE_CACHE_COUNTS['evictions'] += 1
    return fingerprint


UI_TYPES = {"Screen", "WorkSpace", "WindowManager"}


def path_value(raw, library=None):
    return os.path.normcase(os.path.normpath(bpy.path.abspath(raw, library=library)))


def identity(item):
    library = item.library
    return {"type": item.bl_rna.identifier, "name": item.name,
            # Library.filepath is rebased by Blender to the current Main, even
            # for indirect libraries with a non-null parent. Applying parent a
            # second time corrupts relative paths after instance-only linking.
            "library": path_value(library.filepath) if library else None}


def key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def all_ids():
    seen = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type == "COLLECTION":
            for item in getattr(bpy.data, prop.identifier, ()):
                if isinstance(item, bpy.types.ID) and item.as_pointer() not in seen:
                    seen.add(item.as_pointer())
                    if item.bl_rna.identifier not in UI_TYPES:
                        yield item


def value(raw):
    if isinstance(raw, bpy.types.ID):
        return identity(raw)
    if isinstance(raw, float) and not math.isfinite(raw):
        return {"non_finite": repr(raw)}
    if raw is None or isinstance(raw, (str, bool, int, float)):
        return raw
    if isinstance(raw, set):
        return sorted(raw)
    return [value(v) for v in raw]


def properties(item, exclude=()):
    result = {}
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in {"rna_type", "pixels", "tag", *exclude}:
            continue
        if isinstance(item, bpy.types.Image) and name == "file_format":
            continue  # Getter loads the buffer and can discard partial packed-tile state.
        if prop.type in ("BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"):
            result[name] = value(getattr(item, name))
        elif prop.type == "POINTER" and isinstance(getattr(item, name), bpy.types.ID):
            result[name] = identity(getattr(item, name))
    return result


def nested(item, depth=3, seen=()):
    """Bounded RNA configuration, including read-only containers of editable values."""
    if item is None:
        return None
    if isinstance(item, bpy.types.ID):
        return identity(item)
    ptr = item.as_pointer()
    if ptr in seen or depth == 0:
        return {"coverage_boundary": item.bl_rna.identifier}
    out = properties(item)
    for p in item.bl_rna.properties:
        if p.identifier in {"rna_type", "id_data"}:
            continue
        raw = getattr(item, p.identifier)
        if p.type == "POINTER" and raw is not None and not isinstance(raw, bpy.types.ID):
            out[p.identifier] = nested(raw, depth - 1, (*seen, ptr))
        elif p.type == "COLLECTION":
            out[p.identifier] = [nested(v, depth - 1, (*seen, ptr)) for v in raw]
    return out


def custom_value(raw):
    if isinstance(raw, bpy.types.ID):
        return identity(raw)
    if hasattr(raw, "keys"):
        return {k: custom_value(raw[k]) for k in sorted(raw.keys())}
    if hasattr(raw, "to_list"):
        return [custom_value(v) for v in raw.to_list()]
    return value(raw)


def configuration(item):
    out = properties(item)
    for name in ("color_ramp", "mapping", "texture_mapping", "color_mapping", "domain_settings", "flow_settings", "effector_settings", "settings", "collision_settings", "colorspace_settings", "dof", "stereo"):
        raw = getattr(item, name, None)
        if raw is not None:
            out[name] = nested(raw)
    if hasattr(item, "keys"):
        try:
            names = sorted(item.keys())
        except TypeError:  # Some RNA modifier types expose keys() but disallow IDProperties.
            names = []
        out["custom_properties"] = {k: custom_value(item[k]) for k in names if k != "_RNA_UI"}
    return out


def _attribute_buffers(attribute):
    """Return all verified bulk fields or None; never expose partial buffers."""
    schemas = {
        'FLOAT': ('FloatAttributeValue', (('value', 'FLOAT', 0),)),
        'INT': ('IntAttributeValue', (('value', 'INT', 0),)),
        'BOOLEAN': ('BoolAttributeValue', (('value', 'BOOLEAN', 0),)),
        'INT8': ('ByteIntAttributeValue', (('value', 'INT', 0),)),
        'INT16_2D': ('Short2AttributeValue', (('value', 'INT', 2),)),
        'INT32_2D': ('Int2AttributeValue', (('value', 'INT', 2),)),
        'FLOAT2': ('Float2AttributeValue', (('vector', 'FLOAT', 2),)),
        'FLOAT_VECTOR': ('FloatVectorAttributeValue', (('vector', 'FLOAT', 3),)),
        'FLOAT4': ('Float4AttributeValue', (('vector', 'FLOAT', 4),)),
        'QUATERNION': ('QuaternionAttributeValue', (('value', 'FLOAT', 4),)),
        'FLOAT_COLOR': ('FloatColorAttributeValue', (('color', 'FLOAT', 4), ('color_srgb', 'FLOAT', 4))),
        'BYTE_COLOR': ('ByteColorAttributeValue', (('color', 'FLOAT', 4), ('color_srgb', 'FLOAT', 4))),
    }
    data = attribute.data
    count = len(data)
    if not count:
        return
    schema = schemas.get(attribute.data_type)
    if schema is not None and data[0].bl_rna.identifier == schema[0]:
        props = [p for p in data[0].bl_rna.properties
                 if not p.is_readonly and p.identifier not in {'rna_type', 'pixels', 'tag'}]
        if (tuple((p.identifier, p.type, getattr(p, 'array_length', 0)) for p in props) == schema[1]
                and all(tuple(p.array_dimensions) == (p.array_length, 0, 0) for p in props)):
            buffers = []
            try:
                for prop in props:
                    width = prop.array_length or 1
                    buffer = array({'FLOAT': 'f', 'INT': 'i', 'BOOLEAN': 'b'}[prop.type], [0]) * (count * width)
                    data.foreach_get(prop.identifier, buffer)
                    buffers.append((prop.identifier, prop.type, prop.array_length, buffer))
            except (AttributeError, TypeError, ValueError, RuntimeError):
                buffers = []  # Unsupported bulk getter keeps the complete legacy path.
            if buffers:
                return buffers
    return None


def _attribute_payloads(attribute):
    """Bulk-read verified flat numeric RNA fields; preserve legacy JSON payloads."""
    data = attribute.data
    count = len(data)
    buffers = _attribute_buffers(attribute)
    if buffers:
        for index in range(count):
            payload = {}
            for name, kind, width, buffer in buffers:
                raw = buffer[index * width:(index + 1) * width] if width else buffer[index]
                if kind == 'BOOLEAN':
                    raw = [bool(v) for v in raw] if width else bool(raw)
                payload[name] = value(raw)
            yield payload
        return
    for entry in data:
        yield properties(entry)


def _attribute_fingerprint(attribute, chunk_size=8192):
    """Hash the unchanged per-entry canonical JSON/newline stream in chunks.

    Numeric finite fields contain no strings or nested dictionaries, so the
    known array/outer-record separators can be restored without ambiguity.
    Unknown schemas, incomplete getters and non-finite fields use the complete
    original per-entry writer. No native hash or coverage boundary is changed.
    """
    count = len(attribute.data)
    fingerprint = hashlib.sha256()
    if not count:
        return fingerprint.hexdigest(), 0
    buffers = _attribute_buffers(attribute)
    non_finite = (sum(not math.isfinite(v) for _, kind, _, buffer in buffers
                      if kind == 'FLOAT' for v in buffer) if buffers else 0)
    if not buffers or non_finite:
        # Read entries directly: never retry a failed bulk getter or retain a
        # partial color field. Original value() handles non-finite sentinels.
        non_finite = 0
        for entry in attribute.data:
            payload = properties(entry)
            non_finite += sum(not math.isfinite(v) for v in _numbers(payload))
            fingerprint.update((key(payload) + '\n').encode('utf-8'))
        return fingerprint.hexdigest(), non_finite
    cache_key = _attribute_cache_key(attribute, buffers, count)
    if cache_key in _ATTRIBUTE_FINGERPRINT_CACHE:
        fingerprint = _ATTRIBUTE_FINGERPRINT_CACHE.pop(cache_key)
        _ATTRIBUTE_FINGERPRINT_CACHE[cache_key] = fingerprint
        _ATTRIBUTE_CACHE_COUNTS['hits'] += 1
        return fingerprint, 0
    _ATTRIBUTE_CACHE_COUNTS['misses'] += 1
    if len(buffers) == 1:
        name, kind, width, buffer = buffers[0]
        prefix = '{' + key(name) + ':'
        separator = '}\n' + prefix
        for start in range(0, count, chunk_size):
            end = min(start + chunk_size, count)
            if width:
                flat = buffer[start * width:end * width]
                rows = list(zip(*(iter(flat),) * width))
                body = key(rows)[1:-1].replace('],[', ']' + separator + '[')
            else:
                rows = buffer[start:end].tolist()
                if kind == 'BOOLEAN':
                    rows = [bool(v) for v in rows]
                body = key(rows)[1:-1].replace(',', separator)
            fingerprint.update((prefix + body + '}\n').encode('utf-8'))
        return _store_attribute_fingerprint(cache_key, fingerprint.hexdigest()), 0
    # Both finite color fields remain present with their canonical key order.
    for start in range(0, count, chunk_size):
        rows = []
        for index in range(start, min(start + chunk_size, count)):
            payload = {}
            for name, kind, width, buffer in buffers:
                raw = buffer[index * width:(index + 1) * width].tolist() if width else buffer[index]
                if kind == 'BOOLEAN':
                    raw = [bool(v) for v in raw] if width else bool(raw)
                payload[name] = raw
            rows.append(payload)
        fingerprint.update((key(rows)[1:-1].replace('},{', '}\n{') + '\n').encode('utf-8'))
    return _store_attribute_fingerprint(cache_key, fingerprint.hexdigest()), 0



def mesh_content(mesh):
    hashes, non_finite = {}, 0
    def field(name, collection, prop, width, kind):
        nonlocal non_finite
        data = array(kind, [0]) * (len(collection) * width)
        collection.foreach_get(prop, data)
        if kind == "f":
            non_finite += sum(not math.isfinite(v) for v in data)
        hashes[name] = hashlib.sha256(data.tobytes()).hexdigest()
    field("positions", mesh.vertices, "co", 3, "f")
    field("edges", mesh.edges, "vertices", 2, "i")
    field("loop_vertices", mesh.loops, "vertex_index", 1, "i")
    for prop in ("loop_start", "loop_total", "material_index"):
        field(prop, mesh.polygons, prop, 1, "i")
    field("smooth", mesh.polygons, "use_smooth", 1, "b")
    for layer in mesh.uv_layers:
        field("uv:" + layer.name, layer.data, "uv", 2, "f")
    if mesh.shape_keys:
        for block in mesh.shape_keys.key_blocks:
            field("shape:" + block.name, block.data, "co", 3, "f")
    weights = hashlib.sha256()
    for vertex in mesh.vertices:
        entries = [[g.group, value(g.weight)] for g in vertex.groups]
        non_finite += sum(not math.isfinite(g.weight) for g in vertex.groups)
        weights.update((key(entries) + "\n").encode("utf-8"))
    hashes["deform_weights"] = weights.hexdigest()
    attributes = []
    for attribute in mesh.attributes:
        fingerprint, attribute_non_finite = _attribute_fingerprint(attribute)
        non_finite += attribute_non_finite
        attributes.append({"name": attribute.name, "domain": attribute.domain, "data_type": attribute.data_type,
                           "count": len(attribute.data), "sha256": fingerprint})
    return {"counts": {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons)},
            "hashes": hashes, "materials": [identity(m) if m else None for m in mesh.materials],
            "attributes": sorted(attributes, key=key), "non_finite_values": non_finite,
            "diagnostics": {"zero_area_faces": sum(p.area <= 1e-12 for p in mesh.polygons),
                            "zero_length_edges": sum((mesh.vertices[e.vertices[0]].co - mesh.vertices[e.vertices[1]].co).length <= 1e-12 for e in mesh.edges),
                            "invalid_material_indices": sum(p.material_index >= len(mesh.materials) for p in mesh.polygons) if mesh.materials else 0}}


def _numbers(raw):
    if isinstance(raw, dict):
        if "non_finite" in raw:
            yield float(raw["non_finite"])
        else:
            for v in raw.values():
                yield from _numbers(v)
    elif isinstance(raw, (list, tuple)):
        for v in raw:
            yield from _numbers(v)
    elif isinstance(raw, (int, float)):
        yield raw


def node_content(tree):
    nodes = []
    def sockets(collection):
        return [{"identifier": s.identifier, "name": s.name, "type": s.bl_idname,
                 "default": value(s.default_value) if hasattr(s, "default_value") else None} for s in collection]
    for node in tree.nodes:
        row = {"name": node.name, "type": node.bl_idname, "inputs": sockets(node.inputs), "outputs": sockets(node.outputs),
               "properties": {k: v for k, v in configuration(node).items() if k not in {"location", "location_absolute", "width", "height", "select", "show_options", "show_preview", "show_texture"}}}
        if node.bl_idname in ('GeometryNodeRepeatInput', 'GeometryNodeSimulationInput'):
            paired = node.paired_output
            row['zone'] = {'paired_output': {'name': paired.name, 'type': paired.bl_idname} if paired else None}
        elif node.bl_idname in ('GeometryNodeRepeatOutput', 'GeometryNodeSimulationOutput'):
            simulation = node.bl_idname == 'GeometryNodeSimulationOutput'
            items = node.state_items if simulation else node.repeat_items
            item_sockets = [s for s in node.outputs if s.identifier.startswith('Item_')]
            row['zone'] = {'items': [
                {'name': item.name, 'type': item.socket_type, 'order': index,
                 'socket_identifier': item_sockets[index].identifier if index < len(item_sockets) else None,
                 **({'attribute_domain': item.attribute_domain} if simulation else {})}
                for index, item in enumerate(items)]}
        nodes.append(row)
    links = [{"from_node": l.from_node.name, "from_socket": l.from_socket.identifier,
              "to_node": l.to_node.name, "to_socket": l.to_socket.identifier,
              "muted": l.is_muted} for l in tree.links]
    interface = [{"identifier": getattr(i, "identifier", None), "item_type": i.item_type,
                  "properties": properties(i)} for i in tree.interface.items_tree]
    return {"nodes": sorted(nodes, key=key), "links": sorted(links, key=key), "interface": interface}


def curve_content(curve):
    return {"path": curve.data_path, "index": curve.array_index, "extrapolation": curve.extrapolation,
            "points": [{"co": value(p.co), "left": value(p.handle_left), "right": value(p.handle_right),
                        "interpolation": p.interpolation, "left_type": p.handle_left_type, "right_type": p.handle_right_type} for p in curve.keyframe_points],
            "samples": [value(p.co) for p in curve.sampled_points],
            "modifiers": [properties(m) for m in curve.modifiers]}


def record(item, mesh_reader=None):
    result = {**identity(item), "asset_id": None, "asset": None, "fake_user": item.use_fake_user}
    result["custom_properties"] = {k: custom_value(item[k]) for k in sorted(item.keys()) if k != "_RNA_UI"}
    raw_id = item.get("asset_id")
    if raw_id is not None:
        try:
            if not isinstance(raw_id, str) or str(uuid.UUID(raw_id)) != raw_id.lower() or uuid.UUID(raw_id).version != 4:
                raise ValueError("Requires canonical UUID v4")
            result["asset_id"] = str(uuid.UUID(raw_id))
        except ValueError:
            result["asset_id_issue"] = "invalid_uuid"
    if item.asset_data:
        a = item.asset_data
        result["asset"] = {"description": a.description, "author": a.author, "catalog_id": a.catalog_id,
                           "tags": sorted(t.name for t in a.tags)}
    packed_files = getattr(item, "packed_files", ())
    if packed_files:
        result["packed_content"] = [{"tile": getattr(p, "tile_number", None), "bytes": p.packed_file.size,
                                     "sha256": hashlib.sha256(p.packed_file.data).hexdigest()} for p in packed_files if p.packed_file]
    elif getattr(item, "packed_file", None):
        packed = item.packed_file
        result["packed_content"] = [{"bytes": packed.size, "sha256": hashlib.sha256(packed.data).hexdigest()}]
    if isinstance(item, bpy.types.Image):
        result["resource_settings"] = {"source": item.source, "alpha_mode": item.alpha_mode,
                                       "colorspace": item.colorspace_settings.name, "use_view_as_render": item.use_view_as_render}
    elif isinstance(item, (bpy.types.Sound, bpy.types.MovieClip, bpy.types.Volume, bpy.types.CacheFile)):
        result["resource_settings"] = {k: v for k, v in configuration(item).items() if k not in {"filepath", "filepath_raw", "name", "name_full", "asset_data", "use_fake_user"}}
    if isinstance(item, bpy.types.Object):
        result["object"] = {"type": item.type, "data": identity(item.data) if item.data else None,
                            "parent": identity(item.parent) if item.parent else None,
                            "matrix_basis": value(item.matrix_basis), "matrix_parent_inverse": value(item.matrix_parent_inverse),
                            "rotation_mode": item.rotation_mode, "hide_render": item.hide_render,
                            "vertex_groups": [g.name for g in item.vertex_groups],
                            "modifiers": [configuration(m) for m in item.modifiers],
                            "constraints": [configuration(c) for c in item.constraints]}
        if item.pose:
            result["pose"] = [{"name": p.name, "matrix_basis": value(p.matrix_basis),
                               "settings": properties(p), "constraints": [configuration(c) for c in p.constraints]} for p in item.pose.bones]
    if isinstance(item, bpy.types.Mesh):
        result["mesh"] = (mesh_reader or mesh_content)(item)
    if isinstance(item, bpy.types.Curve):
        excluded={"tag"}
        if item.use_auto_texspace:excluded.update(("texspace_location","texspace_size"))
        result["curve"] = {"settings": {k:v for k,v in configuration(item).items() if k not in excluded},
            "materials": [identity(m) if m else None for m in item.materials],
            "splines": [{"settings": properties(s),
                "points": [properties(p) for p in s.points],
                "bezier_points": [properties(p) for p in s.bezier_points]} for s in item.splines]}
        if isinstance(item, bpy.types.TextCurve):
            result["curve"]["format"] = [properties(p) for p in item.body_format]
            result["curve"]["text_boxes"] = [properties(p) for p in item.text_boxes]
    if isinstance(item, bpy.types.Material):
        result["material"] = {"diffuse_color": value(item.diffuse_color), "use_nodes": item.use_nodes}
    if isinstance(item, (bpy.types.Camera, bpy.types.Light, bpy.types.World)):
        result["settings"] = configuration(item)
    tree = item if isinstance(item, bpy.types.NodeTree) else getattr(item, "node_tree", None)
    if tree:
        result["nodes"] = node_content(tree)
    if isinstance(item, bpy.types.Action):
        result["action"] = {"slots": [{"identifier": s.identifier, "target_id_type": s.target_id_type} for s in item.slots],
                            "layers": [{"name": layer.name, "strips": [{"type": strip.type,
                                        "bags": [{"slot_handle": bag.slot_handle, "curves": sorted([curve_content(c) for c in bag.fcurves], key=key)} for bag in getattr(strip, "channelbags", ())]} for strip in layer.strips]} for layer in item.layers]}
    animation = getattr(item, "animation_data", None)
    if animation:
        result["animation"] = {"action": identity(animation.action) if animation.action else None,
                               "slot": animation.action_slot.identifier if animation.action_slot else None,
                               "drivers": [{**curve_content(c), "expression": c.driver.expression, "type": c.driver.type,
                                            "variables": [{"name": v.name, "type": v.type, "targets": [properties(t) for t in v.targets]} for v in c.driver.variables]} for c in animation.drivers],
                               "nla": [{"name": t.name, "mute": t.mute, "is_solo": t.is_solo, "strips": [nla_strip(s) for s in t.strips]} for t in animation.nla_tracks]}
    if isinstance(item, bpy.types.Armature):
        result["bones"] = [{"name": b.name, "parent": b.parent.name if b.parent else None,
                            "matrix_local": value(b.matrix_local), "use_deform": b.use_deform} for b in item.bones]
    if isinstance(item, bpy.types.Scene):
        result["scene"] = {"camera": identity(item.camera) if item.camera else None,
                           "frames": [item.frame_start, item.frame_end], "fps": item.render.fps,
                           "fps_base": item.render.fps_base, "engine": item.render.engine,
                           "resolution": [item.render.resolution_x, item.render.resolution_y, item.render.resolution_percentage]}
    return result


def nla_strip(strip):
    return {"properties": properties(strip), "curves": [curve_content(c) for c in strip.fcurves],
            "modifiers": [configuration(m) for m in strip.modifiers], "strips": [nla_strip(s) for s in strip.strips]}


def snapshot(path, profile=None, *, dependency_only=False):
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
    items = list(all_ids())
    blocks = None if dependency_only else sorted([record(i) for i in items], key=lambda i: key({k: i[k] for k in ("type", "name", "library")}))
    edges = sorted([{"user": identity(user), "target": identity(target)} for target, users in bpy.data.user_map().items()
                    for user in users if target.bl_rna.identifier not in UI_TYPES and user.bl_rna.identifier not in UI_TYPES], key=key)
    edges.extend({"user": identity(item), "target": identity(item.library)} for item in items if item.library)
    edges = [v for _, v in sorted({key(e): e for e in edges}.items())]
    roots = {key(identity(i)) for i in items if isinstance(i, bpy.types.Scene) or i.asset_data}
    reachable = set(roots)
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(key(edge["user"]), set()).add(key(edge["target"]))
    queue = list(roots)
    while queue:
        for neighbor in adjacency.get(queue.pop(), ()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    from dependencies import audit
    dependencies = audit(items, reachable, profile or {})
    if dependency_only:
        return dependencies
    return {"snapshot_version": "1.0", "file": path, "reader_version": bpy.app.version_string,
            "datablocks": blocks, "edges": edges, "dependencies": dependencies,
            "coverage": {"geometry": "positions_topology_uv_weights_shape_keys_RNA_attributes", "nodes": "sockets_links_custom_properties_ramps_mappings_nested_settings_depth3",
                         "animation": "layered_action_keys_drivers_nested_nla_pose_constraints", "evaluation": "explicit_profile_only",
                         "unsupported": ["opaque plugin runtime state", "RNA boundaries are explicit", "render and simulation validity require domain acceptance"]}}


def select(data, selector):
    matches = [b for b in data["datablocks"] if all(b.get(k) == v for k, v in selector.items())]
    if not matches:
        raise Failure("NOT_FOUND", "Selector matched no data-block")
    if len(matches) != 1:
        raise Failure("CONFLICT", "Selector is ambiguous; asset_id must identify exactly one data-block")
    block = matches[0]
    ident = {k: block[k] for k in ("type", "name", "library")}
    return {"selected": block, "selector": selector, "identity_scope": "existing_persistent_id" if "asset_id" in selector else "qualified_name_at_input_hash",
            "uses": [e["target"] for e in data["edges"] if e["user"] == ident],
            "used_by": [e["user"] for e in data["edges"] if e["target"] == ident]}


def compare(before, after):
    def index(data):
        indexed = {}
        persistent = set()
        for block in data["datablocks"]:
            if block["asset_id"]:
                if block["asset_id"] in persistent:
                    raise Failure("CONFLICT", "Duplicate persistent ID prevents unambiguous diff")
                persistent.add(block["asset_id"])
            ident = ("uuid", block["asset_id"], block["library"]) if block["asset_id"] else ("qualified", block["type"], block["name"], block["library"])
            if ident in indexed:
                raise Failure("CONFLICT", "Duplicate persistent ID prevents unambiguous diff")
            indexed[ident] = block
        return indexed
    left, right = index(before), index(after)
    added = [right[k] for k in right.keys() - left.keys()]
    removed = [left[k] for k in left.keys() - right.keys()]
    changed = []
    for ident in left.keys() & right.keys():
        a, b = left[ident], right[ident]
        fields = {field: {"before": a.get(field), "after": b.get(field)} for field in a.keys() | b.keys() if a.get(field) != b.get(field)}
        if fields:
            changed.append({"identity": list(ident), "fields": fields})
    edges_before, edges_after = {key(e) for e in before["edges"]}, {key(e) for e in after["edges"]}
    def dependency_content(report):
        return [{k: v for k, v in dep.items() if k != "raw_path"} for dep in report["items"]]
    dependency_changed = dependency_content(before["dependencies"]) != dependency_content(after["dependencies"])
    return {"equal_within_scope": not (added or removed or changed or edges_before != edges_after or dependency_changed),
            "added": sorted(added, key=key), "removed": sorted(removed, key=key), "changed": sorted(changed, key=key),
            "edges_added": [json.loads(k) for k in sorted(edges_after - edges_before)],
            "edges_removed": [json.loads(k) for k in sorted(edges_before - edges_after)],
            "dependency_audit_changed": dependency_changed,
            "coverage": before["coverage"], "identity_policy": "existing_UUID_else_qualified_name; no rename guessing without UUID"}


def validate_snapshot(data):
    findings = []
    ids = {}
    def non_finite_fields(value, path=""):
        if isinstance(value, dict):
            if set(value) == {"non_finite"}:
                yield path
            else:
                for k, v in value.items():
                    yield from non_finite_fields(v, path + "/" + k)
        elif isinstance(value, list):
            for index, v in enumerate(value):
                yield from non_finite_fields(v, path + "/" + str(index))
    for block in data["datablocks"]:
        ident = {k: block[k] for k in ("type", "name", "library")}
        if block["asset_id"]:
            ids.setdefault(block["asset_id"], []).append(ident)
        if block.get("asset_id_issue"):
            findings.append({"severity": "error", "code": "INVALID_ASSET_ID", "owner": ident})
        if block.get("mesh", {}).get("non_finite_values"):
            findings.append({"severity": "error", "code": "NON_FINITE_GEOMETRY", "owner": ident})
        for issue, count in block.get("mesh", {}).get("diagnostics", {}).items():
            if count:
                findings.append({"severity": "error" if issue == "invalid_material_indices" else "warning", "code": "GEOMETRY_DIAGNOSTIC", "owner": ident, "issue": issue, "count": count})
        for path in non_finite_fields(block):
            findings.append({"severity": "error", "code": "NON_FINITE_PROPERTY", "owner": ident, "path": path})
    for asset_id, owners in ids.items():
        if len(owners) > 1:
            findings.append({"severity": "error", "code": "DUPLICATE_ASSET_ID", "asset_id": asset_id, "owners": owners})
    for dep in data["dependencies"]["items"]:
        if dep["role"] == "output":
            continue
        if dep["status"] in ("missing", "incomplete", "cache_directory_missing"):
            findings.append({"severity": "error" if dep["reachability"] == "scene_or_asset" else "warning",
                             "code": "MISSING_RESOURCE", "resource": dep})
        elif dep["status"] in ("pattern_observed", "pattern_unresolved", "directory_present_unverified"):
            findings.append({"severity": "warning", "code": "RESOURCE_COVERAGE_UNVERIFIED", "resource": dep})
    return {"valid_within_scope": not any(f["severity"] == "error" for f in findings), "findings": findings,
            "dependency_closure": "partial", "publish_ready": False, "coverage": data["coverage"],
            "not_run": ["render", "animation_evaluation", "simulation", "licensing", "asset_index_and_catalog_consistency"]}


def run(command, params, job):
    if command == 'dependency.audit':
        data=snapshot(params['file'],params.get('profile'),dependency_only=True)
        atomic_json(job/'dependencies.json',data)
        return data
    data = snapshot(params["file"], params.get("profile"))
    atomic_json(job / ("before.snapshot.json" if command == "diff" else "snapshot.json"), data)
    if command == "query":
        return select(data, params["selector"]) if "selector" in params else data
    if command == "identity.prepare":
        from verification import prepare_identity
        return prepare_identity(data, params, job)
    if command == "validate":
        report = validate_snapshot(data)
        from verification import extend_validation
        extend_validation(report, data, params.get("profile", {}), job)
        atomic_json(job / "validation.json", report)
        if not report["valid_within_scope"]:
            raise Failure("VALIDATION_FAILED", "Scoped checks failed; see validation.json in the job directory")
        return report
    other = snapshot(params["other"], params.get("profile"))
    atomic_json(job / "after.snapshot.json", other)
    return compare(data, other)
