"""Read-only UV consumer inspection for Blender materials.

This module deliberately understands only a small, explicit set of UV bindings:

* UV Map nodes with an existing, named UV layer.
* Texture Coordinate "UV" outputs, which use the object's active render UV.
* An Image Texture with an unconnected Vector input, which Blender resolves to
  the object's active render UV (verified against Blender 5.2.1 in the tests).
* Reroutes and nested Node Group input/output mapping.

Anything else is reported as *unknown*.  In particular, UV coordinate ranges,
integer UV cells, object custom properties, and node names are never used to
infer intent.  The scan is bounded and never writes Blender data.
"""

import bpy


MODULE_VERSION = "1.0.0"
__all__ = [
    "MODULE_VERSION",
    "SCAN_LIMITS",
    "active_render_name",
    "inspect_objects",
    "guard_import",
    "require_editable_layer",
    "repeat_evidence",
]

# Deliberately conservative.  A real material commonly has fewer than 20
# group levels and fewer than 100 upstream nodes per consumer.
MAX_SHADER_DEPTH = 64
MAX_GROUP_DEPTH = 24
MAX_VISITS = 20000
MAX_CONSUMERS = 1024

SCAN_LIMITS = {
    "max_shader_depth": MAX_SHADER_DEPTH,
    "max_group_depth": MAX_GROUP_DEPTH,
    "max_visits": MAX_VISITS,
    "max_consumers": MAX_CONSUMERS,
}

_TILED_SOURCE = "TILED"


def _ptr(value):
    try:
        return int(value.as_pointer())
    except Exception:
        return id(value)


def _ptr_tuple(values):
    return tuple(_ptr(value) for value in values)


def active_render_name(obj):
    """Return the object's active-render UV layer name, or ``None``."""
    if obj is None or getattr(obj, "type", None) != "MESH":
        return None
    names = [layer.name for layer in obj.data.uv_layers if layer.active_render]
    return names[0] if names else None


def _active_render_names(obj):
    if obj is None or getattr(obj, "type", None) != "MESH":
        return []
    return [layer.name for layer in obj.data.uv_layers if layer.active_render]


def _layer_names(obj):
    if obj is None or getattr(obj, "type", None) != "MESH":
        return []
    return [layer.name for layer in obj.data.uv_layers]


def _warn(state, code):
    if code not in state["warnings"]:
        state["warnings"].append(code)


def _structural_unknown(state, code, **details):
    """Record uncertainty that could conceal an image consumer."""
    _warn(state, code)
    record = {"code": code}
    record.update(details)
    if record not in state["structural_unknowns"]:
        state["structural_unknowns"].append(record)
    state["scan_uncertain"] = True


def _budget_tick(state, code="scan_budget_exhausted"):
    state["visits"] += 1
    if state["visits"] > MAX_VISITS:
        if not state["budget_exhausted"]:
            state["budget_exhausted"] = True
            _structural_unknown(state, code)
        return False
    return True


def _find_socket(sockets, identifier):
    for socket in sockets:
        if socket.identifier == identifier:
            return socket
    # Identifier is the stable interface key.  The name fallback is only for
    # unusual linked data whose identifier cannot be read.
    for socket in sockets:
        if socket.name == identifier:
            return socket
    return None


def _active_group_output(tree, state=None):
    outputs = [n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"]
    active = [n for n in outputs if getattr(n, "is_active_output", False)]
    if len(active) > 1 and state is not None:
        _warn(state, "multiple_active_group_outputs")
    if active:
        return active[0]
    return None


def _image_tiles(image):
    if image is None or getattr(image, "source", None) != _TILED_SOURCE:
        return []
    try:
        return sorted({int(tile.number) for tile in image.tiles})
    except Exception:
        return []


def _unknown_binding(node, reason, kind, depth=0, **extra):
    result = {
        "status": "unknown",
        "binding": "unknown",
        "kind": kind,
        "layer": None,
        "reason": reason,
        "node": getattr(node, "name", None),
        "node_type": getattr(node, "bl_idname", None),
        "trace_depth": depth,
    }
    result.update(extra)
    return result


def _resolved_binding(node, layer, kind, depth=0):
    return {
        "status": "resolved",
        "binding": kind,
        "kind": kind,
        "layer": layer,
        "reason": None,
        "node": getattr(node, "name", None),
        "node_type": getattr(node, "bl_idname", None),
        "trace_depth": depth,
    }


def _active_render_binding(state, obj, node, kind, depth=0):
    names = _active_render_names(obj)
    if len(names) == 1:
        return _resolved_binding(node, names[0], kind, depth)
    if len(names) > 1:
        _warn(state, "multiple_active_render_uv_layers")
        return _unknown_binding(
            node,
            "ambiguous_active_render_uv_layers",
            "implicit_active_render",
            depth,
            candidates=list(names),
        )
    return _unknown_binding(
        node,
        "missing_active_render_uv_layer",
        "implicit_active_render",
        depth,
    )


def _trace_group_input(state, socket, context, depth, visited):
    if not context:
        return _unknown_binding(
            socket.node,
            "external_group_input_without_context",
            "group_mapping",
            depth,
        )
    caller = context[-1]
    bound = _find_socket(caller.inputs, socket.identifier)
    if bound is None:
        return _unknown_binding(
            socket.node,
            "unmatched_group_input",
            "group_mapping",
            depth,
            requested_identifier=socket.identifier,
        )
    links = list(bound.links)
    if len(links) != 1:
        return _unknown_binding(
            socket.node,
            "group_input_unlinked_or_multilink",
            "group_mapping",
            depth,
            link_count=len(links),
        )
    return _trace_vector(
        state,
        links[0].from_socket,
        context[:-1],
        depth + 1,
        visited,
    )


def _trace_group_output(state, node, socket, context, depth, visited):
    tree = getattr(node, "node_tree", None)
    if tree is None:
        return _unknown_binding(node, "missing_group_tree", "group_mapping", depth)
    ancestor_trees = {
        _ptr(instance.node_tree)
        for instance in context
        if getattr(instance, "node_tree", None) is not None
    }
    if _ptr(tree) in ancestor_trees:
        return _unknown_binding(node, "group_cycle_in_binding", "cycle", depth)
    if len(context) + 1 > MAX_GROUP_DEPTH:
        return _unknown_binding(node, "group_depth_budget_exceeded", "budget", depth)
    output = _active_group_output(tree, state)
    if output is None:
        return _unknown_binding(
            node,
            "missing_active_group_output",
            "group_mapping",
            depth,
        )
    if getattr(output, "mute", False):
        return _unknown_binding(
            node,
            "muted_active_group_output",
            "muted",
            depth,
        )
    slot = _find_socket(output.inputs, socket.identifier)
    if slot is None:
        return _unknown_binding(
            node,
            "unmatched_group_output",
            "group_mapping",
            depth,
            requested_identifier=socket.identifier,
        )
    links = list(slot.links)
    if len(links) != 1:
        return _unknown_binding(
            node,
            "group_output_unlinked_or_multilink",
            "group_mapping",
            depth,
            link_count=len(links),
        )
    return _trace_vector(
        state,
        links[0].from_socket,
        context + (node,),
        depth + 1,
        visited,
    )


def _trace_vector(state, socket, context, depth, visited):
    """Trace an upstream vector socket to a UV layer or explicit unknown."""
    if depth > MAX_SHADER_DEPTH:
        return _unknown_binding(
            getattr(socket, "node", None),
            "shader_depth_budget_exceeded",
            "budget",
            depth,
        )
    if not _budget_tick(state):
        return _unknown_binding(
            getattr(socket, "node", None),
            "scan_budget_exhausted",
            "budget",
            depth,
        )
    node = socket.node
    key = (_ptr_tuple(context), _ptr(node), socket.identifier)
    if key in visited:
        _warn(state, "binding_cycle_detected")
        return _unknown_binding(node, "binding_cycle_detected", "cycle", depth)
    visited.add(key)

    if getattr(node, "mute", False):
        return _unknown_binding(node, "muted_node", "muted", depth)

    node_type = node.bl_idname

    if node_type == "ShaderNodeUVMap":
        if getattr(node, "from_instancer", False):
            return _unknown_binding(node, "instancer_uv", "instancer", depth)
        layer = getattr(node, "uv_map", "")
        if not layer:
            return _unknown_binding(node, "missing_explicit_uv_layer", "explicit_uvmap", depth)
        if layer not in _layer_names(state["obj"]):
            return _unknown_binding(
                node,
                "uv_layer_not_found",
                "explicit_uvmap",
                depth,
                requested_layer=layer,
            )
        return _resolved_binding(node, layer, "explicit_uvmap", depth)

    if node_type == "ShaderNodeTexCoord":
        if getattr(node, "from_instancer", False):
            return _unknown_binding(node, "instancer_uv", "instancer", depth)
        if socket.identifier != "UV" and socket.name != "UV":
            return _unknown_binding(
                node,
                "unsupported_texcoord_output",
                "unsupported_mapping",
                depth,
                requested_output=socket.name,
            )
        return _active_render_binding(
            state,
            state["obj"],
            node,
            "implicit_texcoord_uv",
            depth,
        )

    if node_type == "NodeReroute":
        if len(node.inputs) != 1:
            return _unknown_binding(node, "invalid_reroute", "unsupported_mapping", depth)
        links = list(node.inputs[0].links)
        if len(links) != 1:
            return _unknown_binding(
                node,
                "unlinked_or_multilink_reroute",
                "unsupported_mapping",
                depth,
                link_count=len(links),
            )
        return _trace_vector(
            state,
            links[0].from_socket,
            context,
            depth + 1,
            visited,
        )

    if node_type == "NodeGroupInput":
        return _trace_group_input(state, socket, context, depth, visited)

    if node_type == "ShaderNodeGroup":
        return _trace_group_output(state, node, socket, context, depth, visited)

    if node_type == "ShaderNodeMapping":
        return _unknown_binding(
            node,
            "unsupported_mapping_node",
            "unknown_mapping",
            depth,
        )

    if node_type == "ShaderNodeTexImage":
        return _unknown_binding(
            node,
            "image_output_as_vector_not_proven",
            "unsupported_mapping",
            depth,
        )

    if node_type.startswith("ShaderNodeTex"):
        return _unknown_binding(
            node,
            "procedural_vector_input",
            "procedural",
            depth,
        )

    return _unknown_binding(
        node,
        "unsupported_vector_node",
        "unsupported_mapping",
        depth,
    )


def _trace_binding(state, obj, image_node, context):
    if getattr(image_node, "mute", False):
        return _unknown_binding(image_node, "muted_node", "muted", 0)
    vector = None
    for socket in image_node.inputs:
        if socket.name == "Vector":
            vector = socket
            break
    if vector is None:
        return _unknown_binding(image_node, "missing_vector_socket", "unsupported_mapping", 0)
    links = list(vector.links)
    if not links:
        # Verified on Blender 5.2.1: an unconnected Image Texture Vector input
        # uses the object's active render UV layer.  The tests render this.
        return _active_render_binding(
            state,
            obj,
            image_node,
            "default_vector_active_render",
            0,
        )
    if len(links) != 1:
        return _unknown_binding(
            image_node,
            "multilink_image_vector",
            "unsupported_mapping",
            0,
            link_count=len(links),
        )
    return _trace_vector(state, links[0].from_socket, context, 1, set())


def _context_names(context):
    return [getattr(node, "name", "") for node in context]


def _register_consumer(state, material, image_node, context, slot_indices):
    material_ptr = _ptr(material)
    key = (
        material_ptr,
        _ptr_tuple(context),
        _ptr(image_node),
    )
    if key in state["consumer_keys"]:
        return
    if len(state["consumers"]) >= MAX_CONSUMERS:
        _structural_unknown(state, "consumer_budget_exhausted")
        return
    state["consumer_keys"].add(key)
    state["used_image_nodes"].add((material_ptr, _ptr(image_node)))

    binding = _trace_binding(state, state["obj"], image_node, context)
    image = getattr(image_node, "image", None)
    if image is None:
        _warn(state, "image_texture_without_image")
    source = getattr(image, "source", None) if image is not None else None
    consumer = {
        "material": material.name,
        "material_slot_indices": list(slot_indices),
        "group_path": _context_names(context),
        "node": image_node.name,
        "image": image.name if image is not None else None,
        "source": source,
        "tiles": _image_tiles(image),
        "extension": getattr(image_node, "extension", None),
        "binding": binding,
        # Convenience mirror; ``binding`` remains the authoritative record.
        "layer": binding.get("layer") if binding.get("status") == "resolved" else None,
    }
    state["consumers"].append(consumer)


def _walk_group_input(state, material, socket, context, tree_stack, depth, seen, path):
    if not context:
        _structural_unknown(
            state,
            "external_group_input_without_context",
            material=material.name,
            node=getattr(socket.node, "name", None),
        )
        return
    caller = context[-1]
    bound = _find_socket(caller.inputs, socket.identifier)
    if bound is None:
        _structural_unknown(
            state,
            "unmatched_group_input",
            material=material.name,
            node=getattr(socket.node, "name", None),
            requested_identifier=socket.identifier,
        )
        return
    links = list(bound.links)
    if not links:
        _warn(state, "unbound_group_input")
        return
    if len(links) > 1:
        _warn(state, "multilink_group_input")
    for link in links:
        _walk_socket(
            state,
            material,
            link.from_socket,
            context[:-1],
            tree_stack[:-1],
            depth + 1,
            seen,
            path,
        )


def _walk_socket(state, material, socket, context, tree_stack, depth, seen, path):
    """Walk upstream from one output socket, registering reachable images."""
    if depth > MAX_SHADER_DEPTH:
        _structural_unknown(
            state,
            "shader_depth_budget_exceeded",
            material=material.name,
            group_path=_context_names(context),
        )
        return
    if not _budget_tick(state):
        return
    node = socket.node
    key = (_ptr_tuple(context), _ptr(node), socket.identifier)
    if key in path:
        _structural_unknown(
            state,
            "node_cycle_detected",
            material=material.name,
            node=getattr(node, "name", None),
            node_type=getattr(node, "bl_idname", None),
            group_path=_context_names(context),
        )
        return
    if key in seen:
        return
    seen.add(key)
    next_path = path | {key}

    if getattr(node, "mute", False):
        _structural_unknown(
            state,
            "muted_node_on_active_path",
            material=material.name,
            node=getattr(node, "name", None),
            node_type=getattr(node, "bl_idname", None),
            group_path=_context_names(context),
        )
        return

    node_type = node.bl_idname
    if node_type == "ShaderNodeTexImage":
        _register_consumer(state, material, node, context, state["current_slot_indices"])
        # An image Color could be used as another node's Vector.  Keep walking
        # upstream so that every reachable image is reported.
    elif node_type == "ShaderNodeGroup":
        tree = getattr(node, "node_tree", None)
        if tree is None:
            _structural_unknown(
                state,
                "missing_group_tree",
                material=material.name,
                node=node.name,
                group_path=_context_names(context),
            )
            return
        if _ptr(tree) in tree_stack:
            _structural_unknown(
                state,
                "group_cycle_detected",
                material=material.name,
                node=node.name,
                group_path=_context_names(context),
            )
            return
        if len(context) + 1 > MAX_GROUP_DEPTH:
            _structural_unknown(
                state,
                "group_depth_budget_exceeded",
                material=material.name,
                node=node.name,
                group_path=_context_names(context),
            )
            return
        output = _active_group_output(tree, state)
        if output is None:
            _structural_unknown(
                state,
                "missing_active_group_output",
                material=material.name,
                group_tree=tree.name,
                node=node.name,
                group_path=_context_names(context),
            )
            return
        if getattr(output, "mute", False):
            _structural_unknown(
                state,
                "muted_active_group_output",
                material=material.name,
                group_tree=tree.name,
                node=node.name,
                group_path=_context_names(context),
            )
            return
        slot = _find_socket(output.inputs, socket.identifier)
        if slot is None:
            _structural_unknown(
                state,
                "unmatched_group_output",
                material=material.name,
                node=node.name,
                requested_identifier=socket.identifier,
                group_path=_context_names(context),
            )
            return
        for link in slot.links:
            _walk_socket(
                state,
                material,
                link.from_socket,
                context + (node,),
                tree_stack + (_ptr(tree),),
                depth + 1,
                seen,
                next_path,
            )
        return
    elif node_type == "NodeGroupInput":
        _walk_group_input(
            state,
            material,
            socket,
            context,
            tree_stack,
            depth,
            seen,
            next_path,
        )
        return

    for input_socket in node.inputs:
        for link in input_socket.links:
            _walk_socket(
                state,
                material,
                link.from_socket,
                context,
                tree_stack,
                depth + 1,
                seen,
                next_path,
            )


def _collect_inactive_images(state, material, slot_indices):
    """Report image nodes not reached from an active material output.

    This is informational only.  It never protects a UV layer.
    """
    root = getattr(material, "node_tree", None)
    if root is None:
        return
    material_ptr = _ptr(material)
    seen_trees = set()
    seen_nodes = set()
    visits = 0

    def visit(tree, group_path, depth=0):
        nonlocal visits
        if depth > MAX_GROUP_DEPTH:
            _warn(state, "inactive_group_depth_budget_exceeded")
            return
        tree_ptr = _ptr(tree)
        if tree_ptr in seen_trees:
            return
        seen_trees.add(tree_ptr)
        for node in tree.nodes:
            visits += 1
            if visits > MAX_VISITS:
                _warn(state, "inactive_image_scan_budget_exhausted")
                return
            node_ptr = _ptr(node)
            if node.bl_idname == "ShaderNodeTexImage":
                if (material_ptr, node_ptr) not in state["used_image_nodes"]:
                    key = (material_ptr, node_ptr)
                    if key not in seen_nodes:
                        seen_nodes.add(key)
                        image = getattr(node, "image", None)
                        state["inactive_images"].append(
                            {
                                "material": material.name,
                                "material_slot_indices": list(slot_indices),
                                "group_path": list(group_path),
                                "node": node.name,
                                "image": image.name if image is not None else None,
                                "source": getattr(image, "source", None)
                                if image is not None
                                else None,
                                "tiles": _image_tiles(image),
                                "extension": getattr(node, "extension", None),
                                "usage_claim": "not_reachable_from_active_output",
                            }
                        )
            nested = getattr(node, "node_tree", None)
            if nested is not None:
                visit(nested, group_path + [node.name], depth + 1)

    visit(root, [])


def _scan_material(state, material, slot_indices):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        _warn(state, "missing_material_nodes")
        return

    active_outputs = [
        node
        for node in tree.nodes
        if node.bl_idname == "ShaderNodeOutputMaterial"
        and getattr(node, "is_active_output", False)
    ]
    if not active_outputs:
        _warn(state, "missing_active_material_output")
        _collect_inactive_images(state, material, slot_indices)
        return
    if len(active_outputs) > 1:
        _warn(state, "multiple_active_material_outputs")

    state["current_slot_indices"] = tuple(slot_indices)
    seen = set()
    for output in active_outputs:
        if getattr(output, "mute", False):
            _structural_unknown(
                state,
                "muted_active_material_output",
                material=material.name,
                node=output.name,
            )
            continue
        for input_socket in output.inputs:
            for link in input_socket.links:
                _walk_socket(
                    state,
                    material,
                    link.from_socket,
                    (),
                    (_ptr(tree),),
                    1,
                    seen,
                    frozenset(),
                )
    _collect_inactive_images(state, material, slot_indices)


def _material_slots(state, obj):
    """Unique materials with all slot indices, preserving slot order."""
    if obj is None or getattr(obj, "type", None) != "MESH":
        return []
    grouped = {}
    order = []
    slots = list(getattr(obj, "material_slots", []))
    if slots:
        slot_materials = [slot.material for slot in slots]
    else:
        # Fallback for unusual mesh data in tests or linked data where the
        # object-level slot collection is unavailable.
        slot_materials = list(getattr(obj.data, "materials", []))
    for index, material in enumerate(slot_materials):
        state["material_slot_records"].append(
            {
                "index": index,
                "material": material.name if material is not None else None,
            }
        )
        if material is None:
            _warn(state, "empty_material_slot")
            continue
        key = _ptr(material)
        if key not in grouped:
            grouped[key] = [material, []]
            order.append(key)
        grouped[key][1].append(index)
    for key in order:
        if len(grouped[key][1]) > 1:
            _warn(state, "duplicate_material_slot")
    return [(grouped[key][0], grouped[key][1]) for key in order]


def _empty_entry(obj, warnings):
    layer_names = _layer_names(obj)
    active_names = _active_render_names(obj)
    return {
        "object": getattr(obj, "name", None),
        "consumers": [],
        "warnings": list(warnings),
        "protected_udim_layers": [],
        "unknown_binding": False,
        "active_render": active_names[0] if active_names else None,
        "layers": layer_names,
        "unresolved_udim": False,
        "known_tiled_layers": [],
        "unknown_bindings": [],
        "structural_unknowns": [],
        "inactive_images": [],
        "active_render_layers": active_names,
        "layer_details": [
            {
                "name": layer.name,
                "active_render": bool(layer.active_render),
            }
            for layer in (obj.data.uv_layers if getattr(obj, "type", None) == "MESH" else [])
        ],
        "material_slots": [],
        "scan_limits": dict(SCAN_LIMITS),
        "module_version": MODULE_VERSION,
    }


def _inspect_object(obj):
    if obj is None:
        return None
    if getattr(obj, "type", None) != "MESH":
        entry = _empty_entry(obj, ["not_a_mesh_object"])
        entry["unknown_binding"] = False
        return entry

    layer_names = _layer_names(obj)
    active_names = _active_render_names(obj)
    state = {
        "obj": obj,
        "warnings": [],
        "visits": 0,
        "budget_exhausted": False,
        "scan_uncertain": False,
        "consumers": [],
        "consumer_keys": set(),
        "used_image_nodes": set(),
        "inactive_images": [],
        "structural_unknowns": [],
        "material_slot_records": [],
        "current_slot_indices": (),
    }
    if len(active_names) > 1:
        _warn(state, "multiple_active_render_uv_layers")

    for material, slot_indices in _material_slots(state, obj):
        _scan_material(state, material, slot_indices)

    unknown_bindings = []
    known_tiled_layers = set()
    unresolved_tiled = False
    for consumer in state["consumers"]:
        binding = consumer["binding"]
        if consumer["source"] == _TILED_SOURCE:
            if binding.get("status") == "resolved" and binding.get("layer"):
                known_tiled_layers.add(binding["layer"])
            else:
                unresolved_tiled = True
        if binding.get("status") != "resolved":
            unknown_bindings.append(
                {
                    "material": consumer["material"],
                    "group_path": list(consumer["group_path"]),
                    "node": consumer["node"],
                    "image": consumer["image"],
                    "source": consumer["source"],
                    "reason": binding.get("reason"),
                    "kind": binding.get("kind"),
                    "binding": binding,
                }
            )
            _warn(state, f"unknown_binding:{binding.get('kind') or 'unknown'}")

    structural = list(state["structural_unknowns"])
    # A structural gap can conceal an image node, including a TILED one.
    unresolved_udim = bool(unresolved_tiled or state["scan_uncertain"])
    if unresolved_tiled:
        _warn(state, "unresolved_tiled_binding")
    if state["scan_uncertain"]:
        _warn(state, "structural_udim_uncertain")
    if unresolved_tiled:
        protected = sorted(layer_names)
    else:
        protected = sorted(known_tiled_layers)

    unknown_binding = bool(unknown_bindings or structural)
    if not state["consumers"]:
        _warn(state, "no_reachable_image_consumer")

    unresolved_reasons = []
    if unresolved_tiled:
        unresolved_reasons.append("tiled_consumer_binding_unresolved")
    if state["scan_uncertain"]:
        unresolved_reasons.append("structural_scan_uncertain")

    return {
        "object": obj.name,
        "consumers": list(state["consumers"]),
        "warnings": list(state["warnings"]),
        "protected_udim_layers": protected,
        "unknown_binding": unknown_binding,
        "active_render": active_names[0] if active_names else None,
        "layers": layer_names,
        "unresolved_udim": unresolved_udim,
        "unresolved_udim_reasons": unresolved_reasons,
        "known_tiled_layers": sorted(known_tiled_layers),
        "unknown_bindings": unknown_bindings,
        "structural_unknowns": structural,
        "inactive_images": list(state["inactive_images"]),
        "active_render_layers": active_names,
        "layer_details": [
            {
                "name": layer.name,
                "active_render": bool(layer.active_render),
            }
            for layer in obj.data.uv_layers
        ],
        "material_slots": list(state["material_slot_records"]),
        "scan_limits": dict(SCAN_LIMITS),
        "module_version": MODULE_VERSION,
    }


def inspect_objects(objects):
    """Inspect one object or an iterable of objects.

    The returned dictionary is JSON-compatible and contains no UV coordinate
    ranges or intent guesses.  It is keyed by object name.
    """
    if objects is None:
        return {}
    if getattr(objects, "type", None) is not None and hasattr(objects, "name"):
        objects = [objects]
    result = {}
    for obj in objects:
        if obj is None:
            continue
        entry = _inspect_object(obj)
        if entry is not None:
            result[obj.name] = entry
    return result


def _entry_for(obj, inspection):
    if inspection is None:
        return inspect_objects([obj]).get(obj.name)
    if not isinstance(inspection, dict):
        return inspect_objects([obj]).get(obj.name)
    candidate = inspection.get(obj.name)
    if isinstance(candidate, dict) and "consumers" in candidate:
        return candidate
    if "consumers" in inspection and "layers" in inspection:
        return inspection
    return inspect_objects([obj]).get(obj.name)


def guard_import(obj, layer, inspection=None):
    """Decide whether an existing UV layer may be copied to a new candidate.

    The guard never authorizes writing an existing layer or modifying a
    material.  A passing result only authorizes copying that layer's data to a
    newly-created candidate layer.
    """
    entry = _entry_for(obj, inspection)
    if entry is None:
        return {
            "object": getattr(obj, "name", None),
            "layer": layer,
            "status": "unknown",
            "decision": "manual_review_before_new_candidate",
            "reason": "inspection_unavailable",
            "safe_to_write_existing_layer": False,
            "safe_to_copy_to_new_candidate": False,
        }

    layers = list(entry.get("layers") or [])
    consumers = list(entry.get("consumers") or [])
    resolved_on_layer = [
        consumer
        for consumer in consumers
        if consumer.get("binding", {}).get("status") == "resolved"
        and consumer.get("binding", {}).get("layer") == layer
    ]
    tiled_on_layer = [
        consumer for consumer in resolved_on_layer if consumer.get("source") == _TILED_SOURCE
    ]
    protected = list(entry.get("protected_udim_layers") or [])
    unresolved = bool(entry.get("unresolved_udim"))
    unknown = bool(entry.get("unknown_binding"))
    base = {
        "object": getattr(obj, "name", None),
        "layer": layer,
        "safe_to_write_existing_layer": False,
        "consumer_count": len(consumers),
        "warnings": list(entry.get("warnings") or []),
        "protected_udim_layers": protected,
        "unresolved_udim": unresolved,
        "unknown_binding": unknown,
        "tiled_consumers_on_layer": tiled_on_layer,
        "resolved_consumers_on_layer": resolved_on_layer,
        "unknown_bindings": list(entry.get("unknown_bindings") or []),
        "structural_unknowns": list(entry.get("structural_unknowns") or []),
    }

    if layer not in layers:
        return {
            **base,
            "status": "fail",
            "decision": "reject_before_write",
            "reason": "layer_not_present_on_object",
            "safe_to_copy_to_new_candidate": False,
            "message": "The requested layer does not exist on this object.",
        }

    if layer in protected and (tiled_on_layer or unresolved):
        return {
            **base,
            "status": "fail",
            "decision": "reject_before_write",
            "reason": (
                "protected_tiled_consumer"
                if tiled_on_layer
                else "unresolved_tiled_binding_protects_all_layers"
            ),
            "safe_to_copy_to_new_candidate": False,
            "message": (
                "This layer is used, or could be used, by an existing TILED "
                "image source. Refusing import before any write. Create a "
                "separate explicit new layer instead."
            ),
        }

    if unresolved:
        return {
            **base,
            "status": "fail",
            "decision": "reject_before_write",
            "reason": "unresolved_udim_evidence",
            "safe_to_copy_to_new_candidate": False,
            "message": (
                "The scan contains unresolved TILED or structural evidence and "
                "cannot prove this is a non-UDIM source. Refusing import."
            ),
        }

    if unknown:
        return {
            **base,
            "status": "unknown",
            "decision": "manual_review_before_new_candidate",
            "reason": "unknown_consumer_binding",
            "safe_to_copy_to_new_candidate": False,
            "message": (
                "No TILED source is confirmed for this layer, but at least one "
                "reachable binding is unresolved. This layer must not be "
                "presented as a verified non-UDIM source; manual review is "
                "required before copying it to a new candidate."
            ),
        }

    return {
        **base,
        "status": "pass",
        "decision": "allow_new_candidate_copy_only",
        "reason": "no_tiled_consumer_bound_to_layer",
        "safe_to_copy_to_new_candidate": True,
        "message": (
            "No TILED consumer is bound to this layer. Copying to a newly "
            "created candidate layer is allowed; never overwrite this layer or "
            "its material."
        ),
    }


def require_editable_layer(obj, layer, inspection=None):
    """Alias of :func:`guard_import` with an explicit safety name."""
    return guard_import(obj, layer, inspection)


def _unknown_repeat_entries(entry):
    unknown = []
    for item in entry.get("unknown_bindings") or []:
        unknown.append(
            {
                "kind": "consumer_binding_unknown",
                "material": item.get("material"),
                "group_path": list(item.get("group_path") or []),
                "node": item.get("node"),
                "image": item.get("image"),
                "source": item.get("source"),
                "reason": item.get("reason"),
                "binding": item.get("binding"),
            }
        )
    for item in entry.get("structural_unknowns") or []:
        unknown.append(
            {
                "kind": "structural_unknown",
                "code": item.get("code"),
                "detail": item,
            }
        )
    if entry.get("unresolved_udim") and not unknown:
        unknown.append(
            {
                "kind": "unresolved_udim",
                "reasons": list(entry.get("unresolved_udim_reasons") or []),
            }
        )
    return unknown


def repeat_evidence(obj, layer, inspection=None):
    """Report repeat evidence for ``layer`` without reinterpreting its use.

    ``new_tiled_candidate`` is intentionally independent of current evidence:
    choosing a new TILED layer for future painting is allowed, but it must not
    be described as the current source's use.
    """
    entry = _entry_for(obj, inspection)
    future_candidate = {
        "allowed": True,
        "requires_new_layer_name": True,
        "existing_layer_unchanged": True,
        "existing_images_required": False,
        "claim_about_current_source": False,
        "note": (
            "An explicit new TILED candidate may be chosen for future painting. "
            "This does not reinterpret or claim the existing layer's consumers."
        ),
    }
    base = {
        "object": getattr(obj, "name", None),
        "layer": layer,
        "resolved_repeat_consumers": [],
        "contradictions": [],
        "unknown": [],
        "non_repeat_consumers": [],
        "existing_source_use_claimed": False,
        "new_tiled_candidate": future_candidate,
    }
    if entry is None:
        return {
            **base,
            "status": "unknown",
            "current_layer_use": "unknown",
            "reason": "inspection_unavailable",
        }

    layers = list(entry.get("layers") or [])
    if layer not in layers:
        return {
            **base,
            "status": "unknown",
            "current_layer_use": "unknown",
            "reason": "layer_not_present_on_object",
            "warning": "No claim is made about any current source use.",
        }

    consumers = list(entry.get("consumers") or [])
    resolved_on_layer = [
        consumer
        for consumer in consumers
        if consumer.get("binding", {}).get("status") == "resolved"
        and consumer.get("binding", {}).get("layer") == layer
    ]
    tiled = [c for c in resolved_on_layer if c.get("source") == _TILED_SOURCE]
    non_tiled = [c for c in resolved_on_layer if c.get("source") != _TILED_SOURCE]
    repeat = [c for c in non_tiled if c.get("extension") == "REPEAT" and c.get("image")]
    non_repeat = [c for c in non_tiled if c.get("extension") != "REPEAT" and c.get("image")]
    unknown = _unknown_repeat_entries(entry)

    contradictions = []
    if tiled:
        contradictions.append(
            {
                "kind": "existing_tiled_consumer",
                "severity": "critical",
                "message": (
                    "This layer has a resolved TILED image consumer. It must not "
                    "be described as a single-image REPEAT or as a new candidate."
                ),
                "consumers": tiled,
            }
        )
    extensions = sorted(
        {
            str(consumer.get("extension"))
            for consumer in non_tiled
            if consumer.get("extension") is not None
        }
    )
    if len(extensions) > 1:
        contradictions.append(
            {
                "kind": "multiple_extensions_on_layer",
                "severity": "warning",
                "extensions": extensions,
            }
        )
    if repeat and non_repeat:
        contradictions.append(
            {
                "kind": "repeat_and_non_repeat_consumers",
                "severity": "warning",
                "message": "The same resolved layer is used with REPEAT and a non-REPEAT extension.",
            }
        )

    has_unknown = bool(unknown or entry.get("unresolved_udim") or entry.get("unknown_binding"))
    if contradictions:
        status = "fail"
    elif has_unknown:
        status = "unknown"
    elif repeat:
        status = "pass"
    elif non_repeat:
        status = "not_repeat"
    else:
        status = "no_consumer_evidence"

    if tiled and non_tiled:
        current_use = "mixed"
    elif tiled:
        current_use = "existing_tiled"
    elif repeat and non_repeat:
        current_use = "mixed"
    elif repeat:
        current_use = "resolved_repeat"
    elif non_repeat:
        current_use = "resolved_non_repeat"
    elif has_unknown:
        current_use = "unknown"
    else:
        current_use = "no_observed_consumer"

    return {
        **base,
        "status": status,
        "current_layer_use": current_use,
        "reason": None,
        "resolved_repeat_consumers": repeat,
        "non_repeat_consumers": non_repeat,
        "tiled_consumers": tiled,
        "contradictions": contradictions,
        "unknown": unknown,
        "existing_source_use_claimed": any(
            consumer.get("image") for consumer in resolved_on_layer
        ),
        "warnings": list(entry.get("warnings") or []),
    }
