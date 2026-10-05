# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, idempotent layered shader authoring. Never saves the source file."""
import copy
import hashlib
import json
import os

from .contract import WorkflowError

KEY = "mw_id"
STATE = "mw_state_v1"

def export_current_manifest(material):
    from .core_v2 import export_current_manifest as export
    return export(material)

def group_fingerprint(group):
    from .core_v2 import group_fingerprint as fingerprint
    return fingerprint(group)

def get_stored_manifest(material):
    from .core_v2 import get_stored_manifest as get_stored
    return get_stored(material)

def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return list(value)

def _nodes(material):
    result = {}
    for node in material.node_tree.nodes:
        if KEY in node:
            if node[KEY] in result:
                raise WorkflowError("managed_conflict", "Duplicate managed node ID")
            result[node[KEY]] = node
    return result

def capture_managed_state(material):
    if "mw_state_v2" in material:
        from .core_v2 import capture_managed_state as capture_v2
        return capture_v2(material)
    return _capture_managed_state_v1(material)

def _capture_managed_state_v1(material):
    nodes = _nodes(material)
    structure = {key: {"type": node.bl_idname, "parent": node.parent.get(KEY) if node.parent else None} for key, node in nodes.items()}
    for key, node in nodes.items():
        for prop in ("operation", "uv_map", "space", "vector_type", "invert", "interpolation", "projection", "extension", "is_active_output"):
            if hasattr(node, prop): structure[key][prop] = getattr(node, prop)
    links = sorted([ [link.from_node.get(KEY, "external:" + link.from_node.name), link.from_socket.identifier, link.to_node.get(KEY, "external:" + link.to_node.name), link.to_socket.identifier] for link in material.node_tree.links if KEY in link.from_node or KEY in link.to_node])
    fields = {}
    for key, node in nodes.items():
        fields[key] = {"inputs": {socket.identifier: _plain(socket.default_value) for socket in node.inputs if hasattr(socket, "default_value")}, "label": node.label}
        for prop in ("operation", "uv_map", "space", "vector_type"):
            if hasattr(node, prop):
                fields[key][prop] = getattr(node, prop)
        if node.type == "TEX_IMAGE":
            image = node.image
            fields[key]["image"] = None if image is None else {"id": image.get(KEY), "file": os.path.normcase(os.path.abspath(__import__('bpy').path.abspath(image.filepath))), "color_space": image.colorspace_settings.name, "source": image.source}
    return {"structure": structure, "links": links, "fields": fields}

def verify_managed_state(material):
    if "mw_state_v2" in material:
        from .core_v2 import verify_managed_state as verify_v2
        return verify_v2(material)
    if STATE not in material:
        return {"ok": False, "conflicts": ["missing_state"]}
    baseline = json.loads(material[STATE])["snapshot"]
    current = capture_managed_state(material)
    conflicts = [part for part in ("structure", "links", "fields") if current[part] != baseline[part]]
    return {"ok": not conflicts, "conflicts": conflicts, "state": current}

def _flatten(manifest):
    result = {}
    for layer in manifest["layers"]:
        prefix = layer["id"]
        for key in ("name", "enabled", "opacity"):
            result[prefix + "/" + key] = layer[key]
        for group in ("values", "mapping", "channels"):
            for key, value in layer[group].items():
                result[prefix + "/" + group + "/" + key] = value
        result[prefix + "/mask"] = layer["mask"]
    return result

def _shape(manifest):
    return {"target": manifest["target"], "material": manifest["material"], "layers": [{"id": layer["id"], "channels": sorted(layer["channels"]), "mask": layer["mask"] is not None} for layer in manifest["layers"]]}

def _field_state(field, node_ids, snapshot):
    leaf = field.rsplit("/", 1)[-1]
    sockets = {"base_color": "Base Color", "roughness": "Roughness", "metallic": "Metallic", "alpha": "Alpha", "emission_color": "Emission Color", "emission_strength": "Emission Strength", "normal_strength": "Strength", "height_distance": "Distance", "scale": "Scale", "rotation": "Rotation", "translation": "Location"}
    result = {}
    for nid in node_ids:
        data = snapshot["fields"][nid]
        if "/channels/" in field or leaf == "mask": result[nid] = data.get("image")
        elif leaf == "name": result[nid] = data["label"]
        elif leaf in ("enabled", "opacity"): result[nid] = next(iter(data["inputs"].values()))
        elif leaf in sockets:
            socket = sockets[leaf]
            result[nid] = data["inputs"].get(socket)
    return result

def existing_resource_paths(material, manifest):
    if material is not None and "mw_state_v2" in material:
        from .core_v2 import existing_resource_paths as paths_v2
        return paths_v2(material, manifest)
    """Resolve unchanged UI resources to verified packaged image locations.

    This is transport-only: changed texture requests are omitted and manually
    edited image bindings are rejected. Rejecting protects cases where multiple
    channels share one original path and a single path map cannot omit one role.
    Baselines use absolute locations; moving an entire package needs explicit
    migration in a later version, not silent baseline acceptance.
    """
    if material is None or STATE not in material:
        return {}
    old = json.loads(material[STATE])
    current = capture_managed_state(material)
    resources = {}
    for layer in manifest["layers"]:
        for role, spec in list(layer["channels"].items()) + ([("mask", layer["mask"])] if layer["mask"] else []):
            field = layer["id"] + ("/mask" if role == "mask" else "/channels/" + role)
            nid = layer["id"] + "/texture/" + role
            if old["request"].get(field) != spec:
                continue
            baseline = old["snapshot"]["fields"].get(nid, {}).get("image")
            actual = current["fields"].get(nid, {}).get("image")
            if baseline is None or actual != baseline:
                raise WorkflowError("managed_conflict", "Packaged image differs from its managed baseline: " + field)
            path = actual["file"]
            if spec["file"] in resources and resources[spec["file"]] != path:
                raise WorkflowError("ambiguous_resource", "One source path has multiple packaged image locations")
            resources[spec["file"]] = path
    return resources

def _preflight_image_files(files):
    """Decode all unique files before touching materials, slots or node trees.

    images.load can succeed for unreadable bytes with size=(0, 0), so require
    decoded dimensions and pixel access. Each private probe is always removed;
    source/nonmanaged images are never reused or modified during this check.
    """
    import bpy
    paths = sorted({os.path.abspath(path) for path, _spec in files.values()})
    for path in paths:
        probe = None
        try:
            probe = bpy.data.images.load(path, check_existing=False)
            width, height = probe.size
            if probe.source != "FILE" or width <= 0 or height <= 0 or len(probe.pixels) < 4:
                raise ValueError("No decodable static image pixels")
            float(probe.pixels[0])
        except Exception as error:
            raise WorkflowError("image_decode_failed", "Texture is not a decodable static FILE image: " + path) from error
        finally:
            if probe is not None:
                bpy.data.images.remove(probe)

def apply_material(manifest, resource_paths=None):
    from .engine_compat import validate_manifest
    validate_manifest(manifest)
    if manifest.get("schema_version") == "1.1":
        from .core_v2 import apply_material as apply_v2
        return apply_v2(manifest, resource_paths)
    import bpy
    resources = resource_paths or {}
    scene = bpy.data.scenes.get(manifest["context"]["scene"])
    if scene is None or manifest["context"]["view_layer"] not in scene.view_layers:
        raise WorkflowError("invalid_context", "Scene or view layer missing")
    obj = scene.objects.get(manifest["target"]["object"])
    if obj is None or obj.type != "MESH":
        raise WorkflowError("invalid_target", "Target must be an explicit mesh object")
    if obj.name not in scene.view_layers[manifest["context"]["view_layer"]].objects:
        raise WorkflowError("invalid_target", "Target is excluded from the requested view layer")
    if obj.library or obj.data.library or obj.mode != "OBJECT":
        raise WorkflowError("protected_target", "Linked data and non-Object Mode targets are unsupported")
    uv = manifest["target"]["uv_layer"]
    if uv not in obj.data.uv_layers:
        raise WorkflowError("missing_uv", "Requested UV layer is missing")
    slot = manifest["target"]["material_slot"]
    if slot >= len(obj.material_slots) and obj.data.users > 1:
        raise WorkflowError("shared_slot", "Cannot add a material slot to shared mesh data")
    mid = manifest["material"]["id"]
    matches = [m for m in bpy.data.materials if m.get(KEY) == mid]
    if len(matches) > 1:
        raise WorkflowError("managed_conflict", "Duplicate managed material ID")
    material = matches[0] if matches else None
    if material:
        from .engine_compat import validate_material
        validate_material(material, manifest.get('preview', {}).get('engine', 'CYCLES'))
    if material and (material.library or any(s.material == material and (other != obj or index != slot) for other in bpy.data.objects for index, s in enumerate(other.material_slots))):
        raise WorkflowError("shared_material", "Managed material is used by another object or material slot; explicit isolation required")
    old = json.loads(material[STATE]) if material and STATE in material else None
    if material and old is None:
        raise WorkflowError("managed_conflict", "Managed material baseline missing")
    flat = _flatten(manifest)
    changed = sorted(key for key, value in flat.items() if old is None or old["request"].get(key) != value)
    if old and _shape(manifest) != old["shape"]:
        raise WorkflowError("structure_change_unsupported", "M1 preserves layer order, channel presence, target and template; create a new material ID")
    if old and (slot >= len(obj.material_slots) or obj.material_slots[slot].material != material or obj.material_slots[slot].link != "OBJECT"):
        raise WorkflowError("managed_conflict", "Target material slot assignment or OBJECT link was manually changed")
    # Resolve all files and check their immutable content before any scene mutation.
    files = {}
    for layer in manifest["layers"]:
        for role, spec in list(layer["channels"].items()) + ([("mask", layer["mask"])] if layer["mask"] else []):
            path = resources.get(spec["file"], spec["file"])
            if not os.path.isfile(path):
                raise WorkflowError("missing_resource", "Texture file missing: " + path)
            with open(path, "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest.lower() != spec["expected_sha256"].lower():
                raise WorkflowError("resource_sha_mismatch", "Texture SHA mismatch: " + path)
            files[(layer["id"], role)] = (path, spec)
            # A new job may copy unchanged requested textures into a new resource
            # directory. Treat that relocation as an explicit image-field update,
            # with exactly the same three-way conflict check as texture replacement.
            if old:
                field = layer["id"] + ("/mask" if role == "mask" else "/channels/" + role)
                nid = layer["id"] + "/texture/" + role
                previous_image = old["snapshot"]["fields"][nid].get("image")
                desired_path = os.path.normcase(os.path.abspath(path))
                if previous_image is None or previous_image["file"] != desired_path:
                    if field not in changed: changed.append(field)
    changed.sort()
    if old:
        current = capture_managed_state(material)
        if any(current[part] != old["snapshot"][part] for part in ("structure", "links")):
            raise WorkflowError("managed_conflict", "Managed node structure or links were manually changed")
        for field in changed:
            for node_id in old["bindings"].get(field, []):
                if _field_state(field, [node_id], current) != _field_state(field, [node_id], old["snapshot"]):
                    raise WorkflowError("managed_conflict", "Manual edit overlaps requested field: " + field)
    _preflight_image_files(files)
    fresh = material is None
    if fresh:
        material = bpy.data.materials.new(manifest["material"]["name"])
        material[KEY] = mid
        material.use_nodes = True
        material.node_tree.nodes.clear()
    tree = material.node_tree
    nodes = _nodes(material)
    bindings = old["bindings"] if old else {}
    def node(nid, kind, frame=None, location=(0, 0)):
        if nid not in nodes:
            n = tree.nodes.new(kind); n[KEY] = nid; n.name = "MW/" + nid
            if frame: n.parent = frame
            n.location = location; nodes[nid] = n
        return nodes[nid]
    def bind(field, n):
        bindings.setdefault(field, [])
        if n[KEY] not in bindings[field]: bindings[field].append(n[KEY])
        return fresh or field in changed
    def link(a, output, b, input):
        if fresh: tree.links.new(a.outputs[output], b.inputs[input])
    def image(n, lid, role):
        path, spec = files[(lid, role)]
        iid = mid + "/" + lid + "/" + role + "/" + spec["expected_sha256"] + "/" + spec["color_space"]
        candidates = [i for i in bpy.data.images if i.get(KEY) == iid and os.path.normcase(bpy.path.abspath(i.filepath)) == os.path.normcase(path)]
        img = candidates[0] if candidates else bpy.data.images.load(path, check_existing=False)
        if not candidates:
            img[KEY] = iid; img.colorspace_settings.name = spec["color_space"]
        elif img.colorspace_settings.name != spec["color_space"] or img.source != "FILE":
            raise WorkflowError("managed_conflict", "Managed image settings changed")
        if img.source != "FILE": raise WorkflowError("unsupported_image", "Only static FILE images supported")
        previous = n.image; n.image = img
        if previous and previous != img and previous.users == 0 and KEY in previous:
            bpy.data.images.remove(previous)
    previous = None
    for index, layer in enumerate(manifest["layers"]):
        lid = layer["id"]; frame = node(lid + "/frame", "NodeFrame", location=(index * 1700, 0))
        if bind(lid + "/name", frame): frame.label = layer["name"]
        shader = node(lid + "/shader", "ShaderNodeBsdfPrincipled", frame, (1000, 0))
        uvn = node(lid + "/uv", "ShaderNodeUVMap", frame, (0, 0))
        if fresh: uvn.uv_map = uv
        mapping = node(lid + "/mapping", "ShaderNodeMapping", frame, (200, 0)); link(uvn, "UV", mapping, "Vector")
        for field, socket in (("scale", "Scale"), ("rotation", "Rotation"), ("translation", "Location")):
            if bind(lid + "/mapping/" + field, mapping): mapping.inputs[socket].default_value = layer["mapping"][field]
        values = layer["values"]
        sockets = {"base_color": "Base Color", "roughness": "Roughness", "metallic": "Metallic", "alpha": "Alpha", "emission_color": "Emission Color", "emission_strength": "Emission Strength"}
        for field, socket in sockets.items():
            if bind(lid + "/values/" + field, shader): shader.inputs[socket].default_value = values[field]
        textures = {}
        for count, role in enumerate(list(layer["channels"]) + (["mask"] if layer["mask"] else [])):
            tex = node(lid + "/texture/" + role, "ShaderNodeTexImage", frame, (450, -count * 260))
            field = lid + ("/mask" if role == "mask" else "/channels/" + role)
            if bind(field, tex): image(tex, lid, role)
            link(mapping, "Vector", tex, "Vector"); textures[role] = tex
            destination = sockets.get("emission_color" if role == "emission" else role)
            if destination: link(tex, "Color", shader, destination)
        normal = None
        if "normal" in textures:
            normal = node(lid + "/normal", "ShaderNodeNormalMap", frame, (750, -700))
            if fresh: normal.space = "TANGENT"; normal.uv_map = uv
            if bind(lid + "/values/normal_strength", normal): normal.inputs["Strength"].default_value = values["normal_strength"]
            link(textures["normal"], "Color", normal, "Color")
        if "height" in textures:
            bump = node(lid + "/bump", "ShaderNodeBump", frame, (950, -900))
            if bind(lid + "/values/height_distance", bump): bump.inputs["Distance"].default_value = values["height_distance"]
            link(textures["height"], "Color", bump, "Height")
            if normal: link(normal, "Normal", bump, "Normal")
            link(bump, "Normal", shader, "Normal")
        elif normal: link(normal, "Normal", shader, "Normal")
        if index:
            factor = node(lid + "/factor", "ShaderNodeMath", frame, (850, 300))
            if fresh: factor.operation = "MULTIPLY"
            if bind(lid + "/opacity", factor) or bind(lid + "/enabled", factor): factor.inputs[0].default_value = layer["opacity"] if layer["enabled"] else 0.0
            bind(lid + "/enabled", factor)
            if fresh: factor.inputs[1].default_value = 1.0
            if "mask" in textures: link(textures["mask"], "Color", factor, 1)
            mix = node(lid + "/mix", "ShaderNodeMixShader", frame, (1350, 200))
            link(factor, 0, mix, 0); link(previous, 0, mix, 1); link(shader, "BSDF", mix, 2); previous = mix
        else: previous = shader
    output = node("output", "ShaderNodeOutputMaterial", location=(len(manifest["layers"]) * 1700, 0)); link(previous, 0, output, "Surface")
    while len(obj.material_slots) <= slot: obj.data.materials.append(None)
    obj.material_slots[slot].link = "OBJECT"; obj.material_slots[slot].material = material
    snapshot = capture_managed_state(material)
    # Preserve the previous baseline for untouched fields: manual edits remain detectable.
    if old:
        for nid, data in old["snapshot"]["fields"].items():
            preserved = copy.deepcopy(data)
            for field in changed:
                if nid not in bindings.get(field, []): continue
                leaf = field.rsplit("/", 1)[-1]
                if "/channels/" in field or leaf == "mask": preserved["image"] = snapshot["fields"][nid].get("image")
                elif leaf == "name": preserved["label"] = snapshot["fields"][nid]["label"]
                elif leaf in ("opacity", "enabled"):
                    socket = next(iter(snapshot["fields"][nid]["inputs"]))
                    preserved["inputs"][socket] = snapshot["fields"][nid]["inputs"][socket]
                else:
                    for socket, value in snapshot["fields"][nid]["inputs"].items():
                        if _field_state(field, [nid], {"fields": {nid: {**snapshot["fields"][nid], "inputs": {socket: value}}}})[nid] is not None:
                            preserved["inputs"][socket] = value
            snapshot["fields"][nid] = preserved
    material[STATE] = _json({"request": flat, "shape": _shape(manifest), "bindings": bindings, "snapshot": snapshot, "manifest": copy.deepcopy(manifest)})
    return {"material_name": material.name, "material_id": mid, "target": dict(manifest["target"]), "counts": {"nodes": len(nodes), "layers": len(manifest["layers"]), "images": len({n.image.name for n in nodes.values() if n.type == "TEX_IMAGE" and n.image})}, "changed_fields": changed, "reused": not fresh, "limitations": ["M1 fixes layer order and channel presence", "Shared managed materials require explicit isolation", "Base layer enabled/opacity are metadata; base surface is always present", "Texture channels directly drive shader inputs; corresponding constant values are unused fallbacks, not texture multipliers", "Moving an entire saved package changes absolute image baselines and requires explicit migration; M1 does not silently accept it", "Tangent OpenGL normal and bump height only; static FILE images"]}
