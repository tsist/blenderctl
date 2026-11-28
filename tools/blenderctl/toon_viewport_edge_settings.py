"""Sparse PRESET boundary source, LineSet channel and brush route edits."""

import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-edge-sources"
WRITE = "edit-edge-sources"
CATEGORIES = ("outline", "object", "smooth", "material", "selected", "normal_angle", "wireframe")
CHANNELS = {"VISIBLE": "visible_enabled", "HIDDEN": "hidden_enabled"}
ROUTE_FIELDS = {"enabled", "mode", "include_open", "min_angle", "max_angle"}


def normalize_request(document, editions):
    operation = document["operation"]
    changes = {"edge_patches", "intersection_patches", "wire_patches"}
    payload = {key: value for key, value in document.items() if key not in {"group_ids", *changes}}
    require(not {"scope", "frames", "elements", "patch"} & payload.keys(), "INVALID_REQUEST", "Use saved LineSet IDs")
    if operation == READ:
        require(not changes & document.keys(), "INVALID_REQUEST", "Inspection accepts no patches")
        payload.update(operation="inspect", scope={"groups": ["edge-sources"]})
    else:
        require("group_ids" not in document, "INVALID_REQUEST", "Use group inside each edge_patches entry")
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == READ and "group_ids" in document:
        ids = document["group_ids"]
        require(isinstance(ids, list) and 1 <= len(ids) <= 32 and all(contract.name(gid, 128) for gid in ids),
                "INVALID_REQUEST", "group_ids needs 1..32 exact group IDs")
        require(len(ids) == len(set(ids)), "INVALID_REQUEST", "Duplicate group IDs")
        result["group_ids"] = ids
    if operation == WRITE:
        require(bool(changes & document.keys()), "INVALID_REQUEST", "Expected boundary or intersection patches")
        if "edge_patches" in document:
            result["edge_patches"] = normalize_patches(document["edge_patches"])
        if "intersection_patches" in document:
            result["intersection_patches"] = normalize_intersections(document["intersection_patches"])
        if "wire_patches" in document:
            result["wire_patches"] = normalize_wire(document["wire_patches"])
    return result


def normalize_patches(patches):
    require(isinstance(patches, list) and 1 <= len(patches) <= 32, "INVALID_REQUEST", "Expected 1..32 edge patches")
    groups = set()
    for patch in patches:
        require(isinstance(patch, dict) and {"group"} < patch.keys() <= {"group", "enabled", "channels", "routes"}
                and contract.name(patch["group"], 128), "INVALID_REQUEST", "An edge patch needs a group and fields")
        gid = patch["group"]
        require(gid not in groups, "INVALID_REQUEST", "Duplicate edge patch group")
        groups.add(gid)
        if "enabled" in patch:
            require(type(patch["enabled"]) is bool, "INVALID_REQUEST", "Edge profile enabled must be boolean")
        if "channels" in patch:
            values = patch["channels"]
            require(isinstance(values, dict) and 0 < len(values) and values.keys() <= CHANNELS.keys()
                    and all(type(value) is bool for value in values.values()), "INVALID_REQUEST", "Invalid group channel gates")
        if "routes" in patch:
            changes = patch["routes"]
            require(isinstance(changes, list) and 1 <= len(changes) <= 14, "INVALID_REQUEST", "Expected 1..14 sparse route edits")
            pairs = set()
            for route in changes:
                ids = {"category", "channel"}
                require(isinstance(route, dict) and ids < route.keys() <= ids | ROUTE_FIELDS,
                        "INVALID_REQUEST", "A route needs category, channel and changed fields")
                category, channel = route["category"], route["channel"]
                require(category in CATEGORIES and isinstance(channel, str) and channel in CHANNELS,
                        "INVALID_REQUEST", "Unknown boundary category or channel")
                require((category,channel) not in pairs, "INVALID_REQUEST", "Duplicate boundary route")
                pairs.add((category,channel))
                for field in ("enabled", "include_open"):
                    if field in route:
                        require(type(route[field]) is bool, "INVALID_REQUEST", field + " must be boolean")
                if "mode" in route:
                    require(route["mode"] in ("DEFAULT", "SPECIFIC"), "INVALID_REQUEST", "Unknown route brush mode")
                for field in ("min_angle", "max_angle"):
                    if field in route:
                        angle = route[field]
                        require(type(angle) in (int,float) and 0 <= angle <= 180,
                                "INVALID_REQUEST", field + " must be a number in 0..180 degrees")
    return patches


def normalize_intersections(patches):
    require(isinstance(patches, list) and 1 <= len(patches) <= 32, "INVALID_REQUEST",
            "Expected 1..32 intersection source patches")
    groups = set()
    for patch in patches:
        require(isinstance(patch, dict) and {"group"} < patch.keys() <= {"group", "enabled", "routes"}
                and contract.name(patch["group"], 128) and patch["group"] not in groups,
                "INVALID_REQUEST", "An intersection patch needs a unique group and source controls")
        require(type(patch.get("enabled", True)) is bool, "INVALID_REQUEST", "Invalid intersection profile gate")
        if "routes" in patch:
            routes = patch["routes"]
            require(isinstance(routes, dict) and 0 < len(routes) and routes.keys() <= CHANNELS.keys(),
                    "INVALID_REQUEST", "Intersection routes use VISIBLE and HIDDEN")
            for row in routes.values():
                require(isinstance(row, dict) and 0 < len(row) and row.keys() <= {"enabled", "mode"}
                        and type(row.get("enabled", True)) is bool and row.get("mode", "DEFAULT") in ("DEFAULT", "SPECIFIC"),
                        "INVALID_REQUEST", "Intersection routes accept enabled and DEFAULT or SPECIFIC")
        groups.add(patch["group"])
    return patches


def normalize_wire(patches):
    require(isinstance(patches, list) and 1 <= len(patches) <= 32, "INVALID_REQUEST",
            "Expected 1..32 wire source patches")
    groups = set()
    for patch in patches:
        require(isinstance(patch, dict) and {"group"} < patch.keys() <= {"group", "enabled", "visible", "hidden"}
                and contract.name(patch["group"], 128) and patch["group"] not in groups,
                "INVALID_REQUEST", "A wire patch needs a unique group and at least one gate")
        require(all(type(patch[key]) is bool for key in patch.keys() - {"group"}),
                "INVALID_REQUEST", "Wire profile and channel gates must be boolean")
        groups.add(patch["group"])
    return patches


def state(document, preview):
    archived = preview.group_lifecycle.records(document)["archived"]
    groups = {row["uid"]:row for row in document["inputs"]["projection"]["editor_document"]["groups"]}
    rows = preview.edge_sources.records(document)
    return {
        gid: {"label":groups[gid]["label"], "enabled":rows[gid]["enabled"] if gid in rows else None,
              "channels":{channel:group[name] for channel,name in CHANNELS.items()},
              "routes":rows[gid]["routes"] if gid in rows else [],
              "authors":rows[gid]["authors"] if gid in rows else {}}
        for gid,group in groups.items() if gid not in archived
    }


def snapshot(scene, preview, wrapped, gids, protect_subset, saved_elements, *, wire_gids=None):
    from toon_workflow.toon_workflow_addon import joint_workflow

    protected = protect_subset(wrapped, gids, set())
    # The native transaction may materialize the v1 object's two dormant v2
    # routes. Existing route settings, authors and active slots must persist.
    sources = protected["edge_sources"]
    protected["edge_sources"] = {
        row["group"]:{**row,"routes":[route for route in row["routes"]
            if route["category"]!="object" or route["enabled"] or route["slot"] is not None]}
        for row in (sources or {}).get("groups",[])
    }
    if wire_gids:
        protected["wire_sources"] = {row["group"]: row for row in (protected["wire_sources"] or {}).get("groups", [])
                                     if row["group"] not in wire_gids}
    document = wrapped["document"]
    registry = preview.source_bindings.recover(document)
    controls = wrapped["scene_controls"]
    tables = {
        "chain": (controls["chain_controls"] or preview.chain_controls.seed_controls(registry))["brushes"],
        "dash": (controls["dash_patterns"] or preview.dash_controls.defaults(registry))["patterns"],
        "map": (controls["map_amounts"] or preview.map_amount_controls.normalize(
            registry,preview.map_amount_controls.defaults(registry)))["amounts"],
    }
    return protected, registry["actuals"], tables, {
        "saved_elements":saved_elements(scene.toon_viewport_preview),
        "geometry":joint_workflow.non_author_signature(scene),
        "brush_effects":document["request"].get("brush_effects"),
    }


def verify_protection(previous, current):
    before, actuals, tables, preserved = previous
    after, new_actuals, new_tables, new_preserved = current
    require(before==after and preserved==new_preserved, "VALIDATION_FAILED", "Edge edit changed protected scene or author state")
    require(all(new_actuals.get(key)==value for key,value in actuals.items()), "VALIDATION_FAILED",
            "Edge edit changed an existing brush identity or hand-authored source")
    require(all(all(new_tables[name].get(key)==value for key,value in table.items()) for name,table in tables.items()),
            "VALIDATION_FAILED", "Edge edit changed a saved chain, amount or dash control")


def require_edit(scene, preview):
    settings = scene.toon_viewport_preview
    preset = settings.preset_text
    require(scene.library is None and scene.override_library is None and scene.is_editable
            and preset.library is None and preset.get("toon_viewport_preview_owned"),
            "UNSUPPORTED", "Edge edit requires an owned PRESET in a local scene")
    curve = preview.curve_editor
    tree = settings.curve_tree
    if tree is not None and tree.get("toon_viewport_curve_role")==curve.ROLE:
        require(curve.signature(tree,settings)==tree.get("last_signature"), "CONFLICT",
                "Commit saved curve edits before changing boundary sources")


def apply(scene, preview, existing, patches):
    native = []
    for patch in patches:
        gid = patch["group"]
        require(gid in existing, "NOT_FOUND", "No active LineSet group: " + gid)
        fallback = existing[gid]["enabled"]
        native.append({"group":gid,"enabled":patch.get("enabled",True if fallback is None else fallback),
                       "routes":patch.get("routes",[]),**{CHANNELS[key]:value for key,value in patch.get("channels",{}).items()}})
    try:
        return preview.edge_ui.set_sources(scene,native)
    except (ValueError, StopIteration) as error:
        raise Failure("CONFLICT", str(error) or "Saved route is unavailable in this style") from error


def apply_intersections(scene, preview, groups, existing, patches):
    native = []
    for patch in patches:
        gid = patch["group"]
        require(gid in groups, "NOT_FOUND", "No active LineSet group: " + gid)
        fallback = existing.get(gid, {}).get("enabled", True)
        native.append({"group": gid, "enabled": patch.get("enabled", True if "routes" in patch else fallback),
                       "routes": patch.get("routes", {})})
    try:
        return preview.ix_ui.set_sources(scene, native)
    except (ValueError, StopIteration) as error:
        raise Failure("CONFLICT", str(error) or "Intersection route is unavailable in this style") from error


def apply_wire(scene, preview, groups, existing, patches):
    native = []
    for patch in patches:
        gid = patch["group"]
        require(gid in groups, "NOT_FOUND", "No active LineSet group: " + gid)
        require(gid in existing or patch.get("enabled") is True, "CONFLICT",
                "Enable the legacy wire profile explicitly before setting its channel gates")
        before = existing.get(gid, {"group":gid, "enabled":False, "visible":True, "hidden":True})
        native.append({**before, **patch})
    try:
        return preview.wire_ui.set_sources(scene, native)
    except (ValueError, StopIteration) as error:
        raise Failure("CONFLICT", str(error) or "Wire route is unavailable in this style") from error


def work(bpy, context, request, wrapped, author, *, saved_elements, protect_subset, save_candidate, author_sha):
    from toon_workflow import toon_viewport_preview as preview

    scene = context.scene
    current = state(wrapped["document"],preview)
    intersections = preview.ix_sources.records(wrapped["document"])
    wires = preview.wire_sources.records(wrapped["document"])
    response = {"operation":request["operation"],"product":request["product"],"context":request["context"],
        "source":{"file":request["file"],"sha256":request["expected_sha256"],"unchanged":True},
        "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    if request["operation"]==READ:
        state_sha = fingerprint([current, intersections, wires])
        scene_sha = fingerprint(snapshot(scene, preview, wrapped, set(), protect_subset, saved_elements))
        if "verify_state_sha256" in request:
            require(state_sha==request["verify_state_sha256"] and scene_sha==request["verify_scene_sha256"],
                    "VALIDATION_FAILED", "Independent reopen changed saved routes or protected style")
        ids = request.get("group_ids",list(current))
        require(set(ids) <= current.keys(), "NOT_FOUND", "Unknown requested active LineSet group")
        require(digest(request["file"])==request["expected_sha256"], "CONFLICT", "Source changed during inspection")
        return {**response,"groups":{gid:current[gid] for gid in ids},
                "intersection_sources":{gid:intersections[gid] for gid in ids if gid in intersections},
                "wire_sources":{gid:wires[gid] for gid in ids if gid in wires},"author_sha256":author,
                "state_sha256":state_sha,"scene_sha256":scene_sha}

    patches = request.get("edge_patches", [])
    ix_patches = request.get("intersection_patches", [])
    wire_patches = request.get("wire_patches", [])
    wire_gids = {patch["group"] for patch in wire_patches}
    gids = {patch["group"] for patch in patches + ix_patches}
    baseline = snapshot(scene, preview, wrapped, gids | wire_gids, protect_subset, saved_elements, wire_gids=wire_gids)
    require_edit(scene, preview)
    event = apply(scene, preview, current, patches) if patches else None
    ix_event = apply_intersections(scene, preview, current, intersections, ix_patches) if ix_patches else None
    wire_event = apply_wire(scene, preview, current, wires, wire_patches) if wire_patches else None
    candidate = preview.file_authoring.read_scene(scene)
    after = state(candidate["document"],preview)
    after_ix = preview.ix_sources.records(candidate["document"])
    after_wire = preview.wire_sources.records(candidate["document"])
    require(after != current or after_ix != intersections or after_wire != wires,
            "CONFLICT", "The source patch did not change saved settings")
    for patch in patches:
        group = after[patch["group"]]
        if "enabled" in patch:
            require(group["enabled"]==patch["enabled"], "VALIDATION_FAILED", "Edge profile gate did not persist")
        for channel, value in patch.get("channels",{}).items():
            require(group["channels"][channel]==value, "VALIDATION_FAILED", "LineSet channel gate did not persist")
        for route in patch.get("routes",[]):
            member = next(row for row in group["routes"] if row["category"]==route["category"] and row["channel"]==route["channel"])
            require(all(member[key]==value for key,value in route.items()), "VALIDATION_FAILED", "Sparse edge route did not persist")
    for patch in ix_patches:
        group = after_ix[patch["group"]]
        if "enabled" in patch:
            require(group["enabled"] == patch["enabled"], "VALIDATION_FAILED", "Intersection profile did not persist")
        for channel, row in patch.get("routes", {}).items():
            require(all(group["routes"][channel][key] == value for key, value in row.items()),
                    "VALIDATION_FAILED", "Intersection route did not persist")
    for patch in wire_patches:
        row = after_wire[patch["group"]]
        require(all(row[key] == value for key,value in patch.items()),
                "VALIDATION_FAILED", "Wire profile or channel gate did not persist")
    verify_protection(baseline, snapshot(scene, preview, candidate, gids | wire_gids, protect_subset, saved_elements,
                                         wire_gids=wire_gids))
    after_sha = fingerprint(snapshot(scene, preview, candidate, set(), protect_subset, saved_elements))
    after_author = author_sha(scene)
    retained = save_candidate(bpy,request)
    return {**response,"groups":after,"edge_event":event,"intersection_sources":after_ix,"intersection_event":ix_event,
            "wire_sources":after_wire,"wire_event":wire_event,
            "author_before_sha256":author,"author_after_sha256":after_author,
            "state_sha256":fingerprint([after, after_ix, after_wire]),"scene_sha256":after_sha,"candidate_sha256":digest(request["stage"]),
            "retained_orphans":retained,"protected":{"status":"passed","existing_brush_sources":len(baseline[1])}}
