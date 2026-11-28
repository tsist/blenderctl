"""Replay native Toon author graph edits with named nodes and sockets."""
import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract

READ = "inspect-author-graph"
EDIT = "edit-author-graph"
OPERATIONS = (READ, EDIT)
HELP = ("inspect-author-graph reads the owned native graph. edit-author-graph uses graph_actions NEW, USE_TREE {name}, ADD "
        "{type,name}, PATCH {node,values}, SOCKET/SHRINK {node}, LINK {from_node,from_socket,to_node,to_socket,replace}, "
        "SELECT/CURVES {node}, SYNC, RESET, CHANNEL {show}, or APPLY. Editing an existing graph requires its tree name.")
LINE = "TOON_AuthorLine"
LINESET = "TOON_AuthorLineSet"
BRUSH = "TOON_AuthorBrush"
REROUTE = "NodeReroute"
TYPES = {LINE, LINESET, BRUSH, REROUTE}
EDITABLE = {
    LINE: {"author_name", "priority", "enabled", "size_mode", "oversampling", "elements_only",
           "override_antialiasing", "antialiasing", "override_off_screen", "off_screen_distance"},
    LINESET: {"author_name", "group_id", "display_color"}, BRUSH: {"actual_id", "author_channel"},
}
FIELDS = {"NEW": set(), "USE_TREE": {"name"}, "ADD": {"type", "name"}, "PATCH": {"node", "values"},
          "SOCKET": {"node"}, "SHRINK": {"node"}, "APPLY": set(),
          "SELECT": {"node"}, "CURVES": {"node"}, "SYNC": set(), "RESET": set(), "CHANNEL": {"show"},
          "LINK": {"from_node", "from_socket", "to_node", "to_socket", "replace"}}
NAVIGATION = {"SELECT", "CURVES", "SYNC", "RESET", "CHANNEL"}


def normalize_request(document, editions):
    dedicated = {"graph_actions", "tree"}
    operation = document["operation"]
    payload = {key: val for key, val in document.items() if key not in dedicated}
    require(not {"scope", "patch", "elements", "frames"} & payload.keys(),
            "INVALID_REQUEST", "Author graph uses named nodes and sockets")
    if operation == READ:
        require(not dedicated & document.keys(), "INVALID_REQUEST", "Inspection reads the active graph")
        payload.update(operation="inspect", scope={"groups": ["author-graph"]})
    else:
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == READ:
        return result
    actions = document.get("graph_actions")
    require(type(actions) is list and 1 <= len(actions) <= 64, "INVALID_REQUEST", "Expected 1..64 native graph actions")
    for action in actions:
        require(type(action) is dict and isinstance(action.get("action"), str) and action["action"] in FIELDS
                and action.keys() == FIELDS[action["action"]] | {"action"}, "INVALID_REQUEST", "Unexpected graph action fields")
        op = action["action"]
        for field in ("name", "node", "from_node", "to_node", "from_socket", "to_socket"):
            if field in action:
                require(contract.name(action[field], 256), "INVALID_REQUEST", "Expected named graph " + field)
        if op == "ADD":
            require(isinstance(action["type"], str) and action["type"] in TYPES, "INVALID_REQUEST", "Unknown native author node")
        if op == "PATCH":
            require(type(action["values"]) is dict and action["values"], "INVALID_REQUEST", "Expected native node values")
        if op == "LINK":
            require(type(action["replace"]) is bool, "INVALID_REQUEST", "Declare link replacement policy")
        if op == "CHANNEL":
            require(type(action["show"]) is bool, "INVALID_REQUEST", "Choose visible or hidden author channel")
    require(all(row["action"] != "NEW" for row in actions[1:]) and all(row["action"] != "APPLY" for row in actions[:-1]),
            "INVALID_REQUEST", "NEW may start the edit and APPLY may finish it")
    tree = document.get("tree")
    first = actions[0]["action"]
    require((tree is None if first == "NEW" else tree is None and first == "USE_TREE" or contract.name(tree, 256)),
            "INVALID_REQUEST", "Name the existing active author tree")
    return {**result, "graph_actions": actions, "tree": tree}


def tree_state(scene):
    from toon_workflow.toon_viewport_preview import curve_editor

    tree = scene.toon_author_files.author_tree
    if tree is None:
        return None
    nodes = []
    for node in tree.nodes:
        fields = EDITABLE.get(node.bl_idname, set()) | ({"line_id"} if node.bl_idname == LINE else set())
        values = {key: list(node.display_color) if key == "display_color" else getattr(node, key) for key in sorted(fields)}
        nodes.append({"name": node.name, "type": node.bl_idname, "values": values, "location": list(node.location),
                      "label": node.label, "inputs": [socket.name for socket in node.inputs],
                      "outputs": [socket.name for socket in node.outputs]})
    links = [{"from_node": link.from_node.name, "from_socket": link.from_socket.name,
              "to_node": link.to_node.name, "to_socket": link.to_socket.name} for link in tree.links]
    settings = scene.toon_viewport_preview
    mapping = settings.curve_tree
    current = None
    if mapping is not None and mapping.get("toon_viewport_curve_role") == curve_editor.ROLE:
        current = {"identity": mapping["identity"], "group": mapping["selected_group"], "slot": mapping["selected_slot"],
                   "signature": fingerprint(curve_editor.signature(mapping, settings))}
    selection = {"active": tree.nodes.active.name if tree.nodes.active else None,
                 "selected": [node.name for node in tree.nodes if node.select],
                 "group": settings.curve_group, "slot": settings.curve_slot, "curve": current,
                 "channel": "VISIBLE" if settings.show_visible_lines else "HIDDEN",
                 "hidden_brushes": [node.name for node in tree.nodes if node.bl_idname == BRUSH and node.hide]}
    return {"tree": tree.name, "type": tree.bl_idname, "owner": tree.get("owner"), "preset": tree.get("preset_text"),
            "revision": tree.get("revision"), "nodes": nodes, "links": links, "selection": selection}


def select_tree(bpy, scene, graph, active, target_name):
    from toon_workflow.toon_viewport_preview import author_channels

    target = bpy.data.node_groups.get(target_name)
    require(target is not None and target != active and target.bl_idname == graph.TREE
            and target.get("owner") == scene.get("toon_author_graph_owner")
            and target.library is None and target.override_library is None and target.is_editable
            and target.users == int(target.use_fake_user), "CONFLICT", "Expected an independent owned inactive author tree")
    settings = scene.toon_viewport_preview
    require(settings.preset_text is not None and target.get("preset_text") == settings.preset_text.name
            and target.get("revision") == settings.preset_revision,
            "CONFLICT", "The saved alternate graph belongs to an earlier author revision")
    if active is not None:
        active.use_fake_user = True
    target.use_fake_user = False
    scene.toon_author_files.author_tree = target
    author_channels.update_tree(target, None)
    return graph.owned(scene)


def set_values(node, values):
    require(values.keys() <= EDITABLE.get(node.bl_idname, set()), "INVALID_REQUEST", "This node has no requested native values")
    for key, desired in values.items():
        definition = node.bl_rna.properties[key]
        kind = definition.type
        if kind == "STRING":
            valid = contract.name(desired, 256)
        elif kind == "BOOLEAN":
            valid = type(desired) is bool
        elif kind == "ENUM":
            valid = desired in {item.identifier for item in definition.enum_items}
        elif definition.is_array:
            valid = (type(desired) is list and len(desired) == definition.array_length
                     and all(type(item) in (int, float) and definition.hard_min <= item <= definition.hard_max for item in desired))
        else:
            valid = type(desired) in ((int,) if kind == "INT" else (int, float)) and definition.hard_min <= desired <= definition.hard_max
        require(valid, "INVALID_REQUEST", "Invalid author node " + key)
        setattr(node, key, desired)


def apply_actions(bpy, context, graph, actions, expected_tree):
    from toon_workflow.toon_viewport_preview import curve_editor

    scene = context.scene
    tree = graph.owned(scene) if expected_tree else None
    require(tree is None or tree.name == expected_tree, "CONFLICT", "Active saved author graph differs")
    if actions[0]["action"] == "USE_TREE" and not expected_tree:
        require(scene.toon_author_files.author_tree is None, "CONFLICT", "Expected an unassigned author graph")
    for action in actions:
        op = action["action"]
        if op == "NEW":
            tree = graph.create(scene)
        elif op == "USE_TREE":
            tree = select_tree(bpy, scene, graph, tree, action["name"])
        elif op == "ADD":
            require(tree.nodes.get(action["name"]) is None, "CONFLICT", "Author node name already exists")
            node = tree.nodes.new(action["type"])
            node.name = action["name"]
            require(node.name == action["name"], "VALIDATION_FAILED", "Native node name differs")
        elif op == "APPLY":
            graph.apply(scene, tree)
        elif op == "LINK":
            outgoing = tree.nodes[action["from_node"]].outputs[action["from_socket"]]
            incoming = tree.nodes[action["to_node"]].inputs[action["to_socket"]]
            require(outgoing.node != incoming.node and (action["replace"] or not incoming.is_linked),
                    "CONFLICT", "Author link would replace a connection or feed itself")
            tree.links.new(outgoing, incoming)
            require(any(link.from_socket == outgoing for link in incoming.links), "VALIDATION_FAILED", "Native link is absent")
        elif op in NAVIGATION:
            require(tree is not None, "CONFLICT", "Navigation requires the named owned author tree")
            settings = scene.toon_viewport_preview
            mapping = settings.curve_tree
            if mapping is not None and mapping.get("toon_viewport_curve_role") == curve_editor.ROLE and op in {"SELECT", "CURVES", "SYNC"}:
                require(curve_editor.signature(mapping, settings) == mapping["last_signature"],
                        "CONFLICT", "Resolve pending CurveMapping before switching the author target")
            if op == "CHANNEL":
                result = bpy.ops.toon_viewport.show_visible_lines(show=action["show"])
                require(settings.show_visible_lines is action["show"], "VALIDATION_FAILED", "The native author channel differed")
            else:
                result = bpy.ops.toon_viewport.author_graph(action=op, node=action.get("node", ""))
            require(result == {"FINISHED"}, "CONFLICT", "Native author graph navigation did not finish")
        else:
            node = tree.nodes[action["node"]]
            if op == "PATCH":
                set_values(node, action["values"])
            else:
                require(node.bl_idname == LINE, "CONFLICT", "Socket editing requires a Line node")
                require(bpy.ops.toon_viewport.author_graph(action=op, node=node.name) == {"FINISHED"},
                        "VALIDATION_FAILED", "Native socket operation failed")
    return list(actions)


def work(bpy, context, request, author, *, author_sha, save_candidate, read_bindings, dependencies):
    from toon_workflow.toon_viewport_preview import author_graph

    scene = context.scene
    rows = read_bindings()
    refs = dependencies()
    previous = tree_state(scene)
    response = {"operation": request["operation"], "product": request["product"], "context": request["context"],
                "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
                "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    scene_sha = fingerprint([rows, refs])
    if request["operation"] == READ:
        observed = fingerprint(previous)
        require(observed == request.get("verify_state_sha256", observed)
                and scene_sha == request.get("verify_scene_sha256", scene_sha), "VALIDATION_FAILED",
                "Independent reopen changed author nodes, links or scene references")
        return {**response, "graph": previous, "author_sha256": author, "state_sha256": observed, "scene_sha256": scene_sha}
    require(scene.library is None and scene.is_editable, "CONFLICT", "Author graph requires an editable local scene")
    try:
        events = apply_actions(bpy, context, author_graph, request["graph_actions"], request["tree"])
    except (KeyError, ValueError, TypeError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error
    updated = author_sha(scene)
    require(read_bindings() == rows and dependencies() == refs
            and (events[-1]["action"] == "APPLY" or updated == author),
            "VALIDATION_FAILED", "Draft graph changed live line author, material slots or dependencies")
    after = tree_state(scene)
    require(after != previous or updated != author, "CONFLICT", "Author graph action did not change saved data")
    retained = save_candidate(bpy, request)
    return {**response, "graph": after, "events": events, "author_before_sha256": author,
            "author_after_sha256": updated, "scene_sha256": scene_sha, "state_sha256": fingerprint(after),
            "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
            "protected": {"status": "passed", "material_slots_and_dependencies": True}}
