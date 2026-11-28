"""Read and edit the saved PRESET line style in an identified Blender scene.

Public operations:
  inspect  read current author/group/brush/selector/line-function state
  edit     replay group controls, owned-brush width/color, selectors and
           material line-function targets into a new candidate .blend
  manage-elements  edit the saved PRESET list of COLOR/DEPTH line passes
  inspect-line-hierarchy  read saved Line, group and collection identities
  manage-line-hierarchy   commit one native Line action to a new candidate
  inspect-parameter-tracks   discover exact production parameters and sample at a frame
  manage-parameter-tracks    key, remove or toggle a typed native Scene track
  inspect-line-groups  read active and recoverable groups and brush editor targets
  manage-line-groups   change a group's lifecycle, output controls or editor handoff
  inspect-edge-sources read native boundary route and channel settings
  edit-edge-sources    batch sparse LineSet boundary edits into a candidate
  inspect-line-material  read saved used-slot ink policies and draft identities
  edit-line-material     add, update or remove a native joint-render ink policy
  capture-line-template  export a native static template JSON in the job folder
  inspect-line-template  inspect an imported template, bound preflight or saved record
  apply-line-template    explicitly rebind a template and publish a candidate
  reset-line-template    archive the current template record in a candidate
  inspect-style-set      read the current Style Set version, roles, slots and cameras
  edit-style-set         author, import, bind, apply or restore it in a candidate
  export-style-set       write the static Style Set JSON to its job folder
  inspect-style-family   read an existing family or preflight explicit bindings
  edit-style-family      create, import, apply or archive a native Style Family
  export-style-family    save a portable family JSON in its job folder
  inspect-scene-controls read saved Pencil BASIC/PRESET and sequence settings
  edit-scene-controls    change selected controls in a new saved scene copy
  inspect-line-visibility read saved line-function targets and LineSet switches
  edit-line-visibility    edit native draw/mask targets and group visibility rules
  inspect-style-pair     read saved native document and binding recipe paths
  stage-style-pair       stage one or both raw source file paths in a new copy
  load-style-pair        load an explicitly bound raw pair through Pencil
  render-camera  export the saved camera's line image or sequence into the job

Every edit binds the source .blend SHA, edition, exact scene/view layer/frame,
scope and current author SHA. It writes only a new candidate, verifies
non-target state, and requires an independent Blender cold reopen before
publication.
"""

import argparse
from contextlib import ExitStack
import copy
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()


if str(ENTRY.parent) not in sys.path:
    sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import (
    Parser,
    canonical,
    fingerprint,
    guard_file,
    open_saved_context,
    read_document,
    require,
)
from material_projection_handoff import error_payload, run_worker
from protocol import (
    CODES,
    DEFAULT_BLENDER,
    DEFAULT_JOBS,
    SCHEMA,
    Failure,
    atomic_json,
    digest,
    envelope,
    read_json,
    timeout_value,
)
from toon_panel_handoff import PRODUCTS, native_product

import toon_viewport_edge_settings as edges
import toon_viewport_line_groups as tracks
import toon_viewport_line_group_lifecycle as groups
import toon_viewport_line_hierarchy as hierarchy
import toon_viewport_line_material as ink
import toon_viewport_line_template as templates
import toon_viewport_style_library as style_sets
import toon_viewport_style_family as style_families
import toon_viewport_scene_controls as scene_controls
import toon_viewport_line_visibility as visibility
import toon_viewport_style_pair as style_pair
import toon_file_output as file_output
import toon_author_graph_handoff as author_graph
import toon_author_file_handoff as author_file
import toon_workbench_correction as corrections
import toon_workbench_jobs_handoff as workbench_jobs
import toon_viewport_lines_contract as contract


COMMAND = "toon-workflow.viewport-lines"
REOPEN = {
    "edit": "inspect", "manage-elements": "inspect-elements",
    hierarchy.WRITE: hierarchy.READ, tracks.WRITE: tracks.READ,
    groups.WRITE: groups.READ, edges.WRITE: edges.READ, ink.WRITE: ink.READ,
    templates.APPLY: templates.READ, templates.RESET: templates.READ,
    style_sets.EDIT: style_sets.READ,
    style_families.EDIT: style_families.READ,
    scene_controls.WRITE: scene_controls.READ,
    visibility.WRITE: visibility.READ,
    style_pair.STAGE: style_pair.READ, style_pair.LOAD: style_pair.READ,
    file_output.EDIT: file_output.READ,
    author_graph.EDIT: author_graph.READ,
    author_file.EDIT: author_file.READ,
    corrections.EDIT: corrections.READ, corrections.UPGRADE: corrections.READ,
    workbench_jobs.RECEIVE: workbench_jobs.READ,
}
MUTATING = set(REOPEN)
ELEMENT_ACTIONS = {"ADD", "COPY", "UPDATE", "REMOVE", "UP", "DOWN", "SYNC_GROUPS", "SELECT"}
ELEMENT_ID = re.compile(r"[A-Za-z0-9_]{1,32}\Z")


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER, help=argparse.SUPPRESS)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    cli.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=300)
    display = cli.add_mutually_exclusive_group()
    for flag in ("--human", "--compact", "--full"):
        display.add_argument(flag, action="store_true")
    sub = cli.add_subparsers(required=True, parser_class=Parser).add_parser(
        "viewport-lines",
        description=__doc__,
        epilog=(
            "Request fields: operation, edition, file, expected_sha256, context {scene,view_layer,frame}. "
            "inspect and edit also need scope {groups,optional objects/collections/materials}; "
            "edit requires expected_author_sha256 and "
            "patch {groups,brushes,selectors,selector_order,line_functions}. "
            "A selector can use initialize_empty=true to create its first unbound record. "
            "Inspect omits --output; edit and manage-elements require a new absolute .blend --output. "
            "inspect-elements reads the active ID, rows, budget and author SHA without scope. "
            "manage-elements needs expected_author_sha256, no scope, element_actions [{action,id,optional new_id or values}] "
            "or element_budget_mb (65..65536). Actions: ADD COPY UPDATE REMOVE UP DOWN SYNC_GROUPS SELECT. "
            "SELECT saves the active element by its stable id for the next panel action. "
            "Values use the native render element fields (label, type, group_uids, line_set_ids, "
            "visible_lines_on, hidden_lines_on, *_on, background and depth). "
            "inspect-line-hierarchy lists saved Lines, groups and collections without scope. "
            "manage-line-hierarchy needs expected_author_sha256, a new --output and line_action {action,...}. "
            "Use new(name, priority, options), set(line, fields), connect(line, group, index), "
            "move(line, index, direction=-1 or 1), socket/shrink/remove(line), "
            "merge_group(group, visible, hidden), merge_collection(collection, domain or null). "
            "Line options: size_mode, antialiasing, oversampling, off_screen_distance, elements_only. "
            "inspect-parameter-tracks optionally accepts parameter_scope, owner, and sample {target, frame}. "
            "manage-parameter-tracks needs expected_author_sha256, expected_animation_sha256, a new --output, "
            "and parameter_action {action, target, optional frame, value for KEY or enabled for TOGGLE}. "
            "Actions KEY REMOVE TOGGLE. Target fields {scope, owner, path, optional component}. "
            "inspect-line-groups lists active/archived group IDs, output settings and editor brush slots. "
            "manage-line-groups needs expected_author_sha256, a new --output and group_action {action,...}. "
            "Use NEW(label), ARCHIVE/RESTORE(group), SET_OUTPUT(group, line_set_id or elements_only), "
            "or SELECT_EDITOR(group, slot, optional open_curves). "
            "inspect-edge-sources takes optional group_ids. edit-edge-sources requires a new --output, "
            "expected_author_sha256 and edge_patches [{group, optional enabled, channels, routes}]. "
            "Channels are VISIBLE/HIDDEN; route fields are category/channel and enabled, mode, include_open, "
            "min_angle or max_angle. Route values are sparse. "
            "intersection_patches uses {group, optional enabled, optional routes {VISIBLE/HIDDEN: {enabled,mode}}}. "
            "wire_patches applies legacy wire profiles: {group, optional enabled, visible, hidden}. "
            "inspect-line-material accepts target {object,slot,material}, optional policy_id or draft=true. "
            "edit-line-material requires expected_author_sha256, a target, a new --output and "
            "material_action {action: ADD/UPDATE/REMOVE, id, optional patch}. "
            "Use source kind AUTHOR_RGB/NODE_RGBA/UV_IMAGE, group, categories, amount, gain and tint. "
            "capture-line-template requires name and returns a native JSON artifact. "
            "inspect-line-template optionally takes template {file,sha256}; give bindings, mode, conflict "
            "and applicable ink_bindings for a bound preflight. apply-line-template uses the same "
            "template/bindings plus expected_author_sha256 and a new --output. "
            "reset-line-template archives a saved application record in a new --output. "
            "inspect-style-set and export-style-set read the saved Style Set. edit-style-set requires "
            "expected_author_sha256, set_action and a new --output. Use NEW, RECORD, BIND, CAMERA, IMPORT, "
            "BIND_CAMERA, APPLY, RESTORE, REMOVE_BINDING, REMOVE_CAMERA or SELECT_CAMERA. "
            "Use expected_set {id,revision} when replaying a known version. "
            "inspect-style-family accepts preflight=true and explicit saved rebind_slots. "
            "edit-style-family requires family_action and expected_author_sha256 with a new --output. "
            "Actions NEW, NEW_V2, SYNC_SUN, IMPORT, APPLY, ARCHIVE and RESTORE. "
            "RESTORE takes application_receipt {file,sha256} and publishes the verified pre-apply source "
            "as a new candidate only while the applied file still matches its receipt. "
            "APPLY verify_rollback=true checks native restore within the worker and reapplies. "
            + file_output.HELP + " "
            + author_graph.HELP + " "
            + author_file.HELP + " "
            + corrections.HELP + " "
            + workbench_jobs.HELP + " "
            "inspect-scene-controls reads native Pencil properties and saved scene/sequence range. "
            "edit-scene-controls needs expected_author_sha256, expected_controls_sha256 and controls_patch. "
            "edit-line-visibility uses expected_author_sha256, target_steps {action,container,kind,target {name,library}}, "
            "and group_patches {group,hidden_mask or self_intersections}. Inspect the exact saved IDs first. "
            "stage-style-pair takes document or recipe {file,sha256}; load-style-pair takes both and verifies the saved scene bindings. "
            "render-camera uses expected_author_sha256, no scope or --output. Use saved_elements=true "
            "or explicit EXR elements, and optionally frames. Use saved_sequence=true to follow the "
            "saved frame range, step, format and element settings."
        ),
    )
    sub.add_argument("--params", type=Path, required=True, help="UTF-8 JSON request")
    sub.add_argument("--output", type=Path, help="New absolute .blend candidate; omit for inspect")
    return cli


def request_for(args, guards):
    document = read_document(guard_file(args.params.absolute(), guards))
    if isinstance(document, dict) and document.get("operation") in {"inspect-elements", "manage-elements"}:
        request = normalize_element_request(document)
    elif isinstance(document, dict) and document.get("operation") in (hierarchy.READ, hierarchy.WRITE):
        request = hierarchy.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in (tracks.READ, tracks.WRITE):
        request = tracks.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in (groups.READ, groups.WRITE):
        request = groups.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in (edges.READ, edges.WRITE):
        request = edges.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in (ink.READ, ink.WRITE):
        request = ink.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in templates.OPERATIONS:
        request = templates.normalize_request(document, tuple(PRODUCTS))
        templates.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in style_sets.OPERATIONS:
        request = style_sets.normalize_request(document, tuple(PRODUCTS))
        style_sets.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in style_families.OPERATIONS:
        request = style_families.normalize_request(document, tuple(PRODUCTS))
        style_families.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in file_output.OPERATIONS:
        request = file_output.normalize_request(document, tuple(PRODUCTS))
        file_output.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in author_graph.OPERATIONS:
        request = author_graph.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in author_file.OPERATIONS:
        request = author_file.normalize_request(document, tuple(PRODUCTS))
        author_file.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in corrections.OPERATIONS:
        request = corrections.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in workbench_jobs.OPERATIONS:
        request = workbench_jobs.normalize_request(document, tuple(PRODUCTS))
        workbench_jobs.guard_inputs(request, guards)
    elif isinstance(document, dict) and document.get("operation") in (scene_controls.READ, scene_controls.WRITE):
        request = scene_controls.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in (visibility.READ, visibility.WRITE):
        request = visibility.normalize_request(document, tuple(PRODUCTS))
    elif isinstance(document, dict) and document.get("operation") in style_pair.OPERATIONS:
        request = style_pair.normalize_request(document, tuple(PRODUCTS))
        style_pair.guard_inputs(request, guards)
    else:
        saved = isinstance(document, dict) and "saved_elements" in document
        sequence = isinstance(document, dict) and "saved_sequence" in document
        if sequence:
            require(document.pop("saved_sequence") is True and document.get("operation") == "render-camera"
                    and not {"frames", "elements", "saved_elements"} & document.keys(),
                    "INVALID_REQUEST", "saved_sequence=true uses the saved range, format and elements")
        if saved:
            require(document.pop("saved_elements") is True and document.get("operation") == "render-camera"
                    and "elements" not in document, "INVALID_REQUEST",
                    "saved_elements=true is only available for render-camera without explicit elements")
        request = contract.normalize_request(document, tuple(PRODUCTS))
        if saved:
            request["saved_elements"] = True
        if sequence:
            request["saved_sequence"] = True
    if "family_restore" not in request:
        request["file"] = str(guard_file(request["file"], guards, request["expected_sha256"]))
    request["product"] = native_product(request, guards)
    if request["operation"] == file_output.RENDER:
        require(args.output is not None and args.output.is_absolute(), "INVALID_REQUEST", "Supply a new absolute output directory")
    elif request["operation"] not in MUTATING:
        require(args.output is None, "INVALID_REQUEST", "Only edits use --output")
    else:
        require(
            args.output is not None
            and args.output.is_absolute()
            and args.output.suffix.lower() == ".blend",
            "INVALID_REQUEST",
            "Editing requires a new absolute .blend --output",
        )
    return request


def normalize_element_request(document):
    if document["operation"] == "inspect-elements":
        require("scope" not in document, "INVALID_REQUEST", "inspect-elements reads the scene list without a scope")
        result = contract.normalize_request({**document, "operation": "inspect", "scope": {"groups": ["elements"]}}, tuple(PRODUCTS))
        result.pop("scope")
        result["operation"] = "inspect-elements"
        return result
    payload = {key: value for key, value in document.items() if key not in {"element_actions", "element_budget_mb"}}
    require(not {"frames", "elements"} & payload.keys(), "INVALID_REQUEST", "Rendering parameters belong to render-camera")
    # The existing no-scope path already validates the saved source, host,
    # context and required author fingerprint.
    result = contract.normalize_request({**payload, "operation": "render-camera"}, tuple(PRODUCTS))
    result["operation"] = "manage-elements"
    actions = document.get("element_actions", [])
    require(isinstance(actions, list) and len(actions) <= 128, "INVALID_REQUEST", "Expected up to 128 element actions")
    for step in actions:
        require(isinstance(step, dict) and {"action", "id"} <= step.keys(), "INVALID_REQUEST", "Element actions need action and id")
        action = step["action"]
        require(isinstance(action, str) and action in ELEMENT_ACTIONS, "INVALID_REQUEST", "Unknown element action")
        expected = {"action", "id"}
        if action == "COPY":
            expected.add("new_id")
        if action == "UPDATE":
            expected.add("values")
        allowed = expected | ({"values"} if action == "ADD" else set())
        require(expected <= step.keys() <= allowed, "INVALID_REQUEST", "Unexpected fields for element action " + action)
        for key in ("id", "new_id"):
            if key in step:
                require(isinstance(step[key], str) and ELEMENT_ID.fullmatch(step[key]), "INVALID_REQUEST",
                        "Use a stable ASCII element ID (1..32 letters, digits or underscores)")
        if "values" in step:
            require(isinstance(step["values"], dict) and step["values"]
                    and not {"id", "pass_name", "classification_profile"} & step["values"].keys(),
                    "INVALID_REQUEST", "Element values must be a nonempty settings patch without identity fields")
    if "element_budget_mb" in document:
        budget = document["element_budget_mb"]
        require(type(budget) is int and 65 <= budget <= 65536, "INVALID_REQUEST", "element_budget_mb must be in 65..65536")
        result["element_budget_mb"] = budget
    require(actions or "element_budget_mb" in result, "INVALID_REQUEST", "Specify element actions or a buffer budget")
    result["element_actions"] = actions
    return result


def _local_ref(block):
    require(
        block is not None and block.library is None,
        "UNSUPPORTED",
        "viewport-lines v1 requires local datablocks; linked data is not editable",
    )
    return {"name": block.name_full, "library": None}


def _resolve_refs(scene, scope):
    import bpy

    result = {}
    for kind, blocks in (
        ("objects", scene.objects),
        ("collections", bpy.data.collections),
        ("materials", bpy.data.materials),
    ):
        resolved = []
        for item in scope[kind]:
            block = blocks.get(item["name"])
            require(block is not None, "NOT_FOUND", "Missing " + kind + " identity: " + item["name"])
            require(block.library is None, "UNSUPPORTED", "Linked " + kind + " are outside v1 scope")
            if kind == "objects":
                require(block.type == "MESH", "CONFLICT", "Selector object is not a mesh: " + block.name)
            resolved.append(_local_ref(block))
        result[kind] = resolved
    return result


def _group_maps(wrapped):
    document = wrapped["document"]
    id_to_label = {}
    for editor in document["inputs"]["projection"]["editor_document"]["groups"]:
        id_to_label[editor["uid"]] = editor.get("label") or editor["uid"]
    controls = wrapped.get("scene_controls", {}).get("groups", [])
    for row in controls:
        id_to_label[row["name"]] = row.get("label") or id_to_label.get(row["name"], row["name"])
    label_to_id = {}
    for gid, label in id_to_label.items():
        label_to_id.setdefault(label, []).append(gid)
    return id_to_label, {label: ids[0] for label, ids in label_to_id.items() if len(ids) == 1}


def _scope_group_ids(wrapped, scope):
    document = wrapped["document"]
    known = {group["id"] for group in document["request"]["boundary"]["groups"]}
    id_to_label, label_to_id = _group_maps(wrapped)
    ids = []
    for key in scope["groups"]:
        if key in known:
            gid = key
        else:
            gid = label_to_id.get(key)
        require(gid in known, "CONFLICT", "Unknown or ambiguous scope group: " + key)
        require(gid not in ids, "INVALID_REQUEST", "Duplicate scope group: " + key)
        ids.append(gid)
    return ids, id_to_label


def _selector_rows(document):
    from toon_workflow.toon_viewport_preview import selector_bindings

    rows, order = selector_bindings.records(document)
    return rows, order


def _used_materials(scene, wrapped, scope_group_ids):
    import bpy

    document = wrapped["document"]
    rows, _order = _selector_rows(document)
    boundaries = {group["id"]: group for group in document["request"]["boundary"]["groups"]}
    used = set()
    for gid in scope_group_ids:
        row = rows.get(gid)
        selected_objects = set()
        selected_materials = set()
        selected_collections = set()
        if row is not None and row.get("enabled", True):
            selected_objects = {item["name"] for item in row.get("objects", []) if item["library"] is None}
            selected_collections = {item["name"] for item in row.get("collections", []) if item["library"] is None}
            selected_materials = {item["name"] for item in row.get("materials", []) if item["library"] is None}
        else:
            boundary = boundaries[gid]
            selected_objects = set(boundary.get("object_ids") or [])
            selected_materials = set(boundary.get("material_names") or [])
        for obj in scene.objects:
            if obj.type != "MESH" or obj.library is not None:
                continue
            object_hit = obj.name in selected_objects
            collection_hit = any(
                collection.name in selected_collections for collection in obj.users_collection
            )
            for slot in obj.material_slots:
                mat = slot.material
                if mat is not None and mat.library is None and (object_hit or collection_hit or mat.name_full in selected_materials):
                    used.add(mat.name_full)
    return used


def _bindings(bpy):
    from inspection import path_value

    rows = []
    for obj in bpy.data.objects:
        rows.append(
            {
                "object": obj.name_full,
                "library": path_value(obj.library.filepath) if obj.library else None,
                "mesh": obj.data.name_full if obj.data else None,
                "active_slot": obj.active_material_index,
                "materials": [
                    [slot.link, slot.material.name_full if slot.material else None]
                    for slot in obj.material_slots
                ],
            }
        )
    return sorted(rows, key=lambda row: (row["object"], row["library"] or ""))


def _dependencies(bpy):
    from inspection import path_value

    return sorted({path_value(path, owner.library) for owner, paths in bpy.data.file_path_map().items()
                   for path in paths} | {path_value(library.filepath) for library in bpy.data.libraries})


def _state_subset(wrapped):
    document = wrapped["document"]
    request = document["request"]
    controls = wrapped.get("scene_controls", {})
    return {
        "groups": request["boundary"]["groups"],
        "selectors": request.get("selector_bindings"),
        "line_hierarchy": request.get("line_hierarchy"),
        "line_functions": request.get("line_functions"),
        "edge_sources": request.get("edge_sources"),
        "intersection_sources": request.get("intersection_sources"),
        "wire_sources": request.get("wire_sources"),
        "recipe": wrapped.get("recipe"),
        "scene_controls": {
            key: controls.get(key)
            for key in ("groups", "settings", "elements", "chain_controls", "map_amounts", "dash_patterns")
        },
        "editor_groups": document["inputs"]["projection"]["editor_document"]["groups"],
    }


def _protected_subset(bpy, wrapped, scope_group_ids, target_container_ids):
    document = wrapped["document"]
    request = document["request"]
    rows, _order = _selector_rows(document)
    functions = request.get("line_functions") or {}
    controls = wrapped.get("scene_controls", {})
    def filtered_groups(value):
        if value is None:
            return None
        result = copy.deepcopy(value)
        result["groups"] = [
            row for row in result.get("groups", []) if row.get("group") not in scope_group_ids
        ]
        return result

    return {
        "groups": [
            group
            for group in request["boundary"]["groups"]
            if group["id"] not in scope_group_ids
        ],
        "selectors": [
            row for gid, row in rows.items() if gid not in scope_group_ids
        ],
        "line_functions": {
            "containers": [
                row
                for row in functions.get("containers", [])
                if row.get("id") not in target_container_ids
            ],
            "masks": functions.get("masks", []),
            "self_intersections": functions.get("self_intersections", []),
        },
        "line_hierarchy": request.get("line_hierarchy"),
        "edge_sources": filtered_groups(request.get("edge_sources")),
        "intersection_sources": filtered_groups(request.get("intersection_sources")),
        "wire_sources": request.get("wire_sources"),
        "editor_groups": [
            group
            for group in document["inputs"]["projection"]["editor_document"]["groups"]
            if group.get("uid") not in scope_group_ids
        ],
        "group_lifecycle": (
            None
            if request.get("group_lifecycle") is None
            else {
                **copy.deepcopy(request["group_lifecycle"]),
                "authored": [
                    row
                    for row in request["group_lifecycle"].get("authored", [])
                    if row.get("group") not in scope_group_ids
                ],
            }
        ),
        "boundary_aliases": [
            row
            for row in request.get("boundary_aliases", [])
            if row.get("group") not in scope_group_ids
        ],
        "intersection_aliases": [
            row
            for row in request.get("intersection_aliases", [])
            if row.get("group") not in scope_group_ids
        ],
        "layers": [
            row for row in request.get("layers", []) if row.get("group_id") not in scope_group_ids
        ],
        "recipe": wrapped.get("recipe"),
        "scene_controls": {
            "groups": [
                row
                for row in controls.get("groups", [])
                if row.get("name") not in scope_group_ids
            ],
            "settings": controls.get("settings"),
            "elements": controls.get("elements"),
        },
        "bindings": _bindings(bpy),
        "dependencies": _dependencies(bpy),
    }


def _changed_sections(before, after):
    return sorted(
        key
        for key in set(before) | set(after)
        if fingerprint(before.get(key)) != fingerprint(after.get(key))
    )


def _author_sha256(scene):
    from toon_workflow.toon_workflow_addon import joint_workflow
    from toon_workflow.toon_workflow_addon.workflow_contract import fingerprint

    author = joint_workflow.capture_author(scene)
    return fingerprint(author)


def _summary(wrapped, scope_group_ids, id_to_label, used_materials, scope_materials):
    import bpy

    from toon_workflow.toon_viewport_preview import (
        edge_sources,
        group_lifecycle,
        line_functions,
        selector_bindings,
    )

    document = wrapped["document"]
    controls = wrapped.get("scene_controls", {})
    control_rows = {row["name"]: row for row in controls.get("groups", [])}
    rows, order = selector_bindings.records(document)
    owned = group_lifecycle.owned(document)
    groups = []
    for group in document["request"]["boundary"]["groups"]:
        control = control_rows.get(group["id"], {})
        selector = rows.get(group["id"])
        groups.append(
            {
                "group": group["id"],
                "label": id_to_label.get(group["id"], group["id"]),
                "in_scope": group["id"] in scope_group_ids,
                "owned": group["id"] in owned,
                "enabled": control.get("enabled"),
                "width_scale": control.get("width_scale"),
                "opacity": control.get("opacity"),
                "line_set_id": control.get("line_set_id"),
                "elements_only": control.get("elements_only"),
                "selectors": None
                if selector is None
                else {
                    "enabled": selector.get("enabled"),
                    "objects": selector.get("objects", []),
                    "collections": selector.get("collections", []),
                    "materials": selector.get("materials", []),
                },
            }
        )
    brushes = []
    for gid in scope_group_ids:
        editor = next(
            (
                row
                for row in document["inputs"]["projection"]["editor_document"]["groups"]
                if row["uid"] == gid
            ),
            None,
        )
        if gid not in owned or editor is None:
            continue
        for actual, meta in owned[gid]["brushes"].items():
            slot = edge_sources.slot_for(actual)
            brush = editor.get("brushes", {}).get(slot)
            if brush is None:
                continue
            brushes.append(
                {
                    "group": gid,
                    "actual": actual,
                    "channel": meta.get("channel"),
                    "role": meta.get("role"),
                    "native_size": brush.get("native_size"),
                    "blend_amount": brush.get("blend_amount"),
                    "color": brush.get("color"),
                }
            )
    functions = line_functions.records(document)
    containers = []
    if functions is not None:
        for row in functions.get("containers", []):
            names = {item["name"] for item in row.get("materials", [])}
            containers.append(
                {
                    "id": row["id"],
                    "materials": row.get("materials", []),
                    "in_scope": names <= scope_materials,
                    "active": names <= used_materials,
                    "disable_intersection": row.get("disable_intersection"),
                    "draw_hidden_lines": row.get("draw_hidden_lines"),
                    "draw_hidden_lines_of_targets": row.get("draw_hidden_lines_of_targets"),
                    "mask_hidden_lines_of_targets": row.get("mask_hidden_lines_of_targets"),
                    "overrides": row.get("overrides"),
                }
            )
    return {
        "groups": groups,
        "group_order": order,
        "brushes": brushes,
        "line_functions": containers,
        "used_materials": sorted(used_materials),
        "scope_materials": sorted(scope_materials),
        "scene_settings": {
            key: _plain(getattr(bpy.context.scene.toon_viewport_preview, key))
            for key in ("enabled", "style_mode", "visibility", "line_width", "opacity")
            if hasattr(bpy.context.scene.toon_viewport_preview, key)
        },
    }


def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__len__"):
        return [_plain(item) for item in value]
    return value


def _same_refs(left, right):
    key = lambda row: (row.get("name"), row.get("library"))
    return sorted(left or [], key=key) == sorted(right or [], key=key)


def _verify_changes(before, after, patch):
    from toon_workflow.toon_viewport_preview import edge_sources, line_functions

    before_controls = {
        row["name"]: row for row in before.get("scene_controls", {}).get("groups", [])
    }
    after_controls = {
        row["name"]: row for row in after.get("scene_controls", {}).get("groups", [])
    }
    for row in patch.get("groups", []):
        gid = row["_group_id"]
        actual = after_controls.get(gid)
        require(actual is not None, "VALIDATION_FAILED", "Group control disappeared after import")
        for field in ("width_scale", "opacity", "enabled"):
            if field in row:
                require(
                    actual.get(field) == row[field]
                    or (
                        isinstance(row[field], float)
                        and abs(float(actual.get(field)) - row[field]) <= 1e-6
                    ),
                    "VALIDATION_FAILED",
                    "Group control did not read back: " + gid + " / " + field,
                )
    for row in patch.get("brushes", []):
        gid, actual_id = row["_group_id"], row["_actual"]
        editor = next(
            (
                item
                for item in after["document"]["inputs"]["projection"]["editor_document"]["groups"]
                if item["uid"] == gid
            ),
            None,
        )
        require(editor is not None, "VALIDATION_FAILED", "Brush group disappeared after import")
        brush = editor.get("brushes", {}).get(edge_sources.slot_for(actual_id))
        require(brush is not None, "VALIDATION_FAILED", "Owned brush disappeared after import")
        for field in ("native_size", "blend_amount"):
            if field in row:
                require(
                    abs(float(brush.get(field)) - float(row[field])) <= 1e-6,
                    "VALIDATION_FAILED",
                    "Brush value did not read back: " + field,
                )
        if "color" in row:
            require(
                all(abs(float(a) - float(b)) <= 1e-6 for a, b in zip(brush.get("color"), row["color"])),
                "VALIDATION_FAILED",
                "Brush color did not read back",
            )
    for row in patch.get("selectors", []):
        gid = row["_group_id"]
        rows, _order = _selector_rows(after["document"])
        actual = rows.get(gid)
        require(actual is not None, "VALIDATION_FAILED", "Selector group disappeared after import")
        for field in ("enabled", "objects", "collections", "materials"):
            if field in row:
                equal = (
                    _same_refs(actual.get(field), row[field])
                    if field in ("objects", "collections", "materials")
                    else actual.get(field) == row[field]
                )
                require(
                    equal,
                    "VALIDATION_FAILED",
                    "Selector did not read back: " + gid + " / " + field,
                )
    patch_functions = patch.get("line_functions")
    if patch_functions:
        records = line_functions.records(after["document"]) or {"containers": []}
        by_id = {row["id"]: row for row in records["containers"]}
        for row in patch_functions.get("containers", []):
            actual = by_id.get(row["id"])
            require(actual is not None, "VALIDATION_FAILED", "Line-function container disappeared")
            for field in (
                "materials",
                "disable_intersection",
                "draw_hidden_lines",
                "draw_hidden_lines_of_targets",
                "mask_hidden_lines_of_targets",
            ):
                if field in row:
                    equal = (
                        _same_refs(actual.get(field), row[field])
                        if field == "materials"
                        else actual.get(field) == row[field]
                    )
                    require(
                        equal,
                        "VALIDATION_FAILED",
                        "Line-function field did not read back: " + row["id"] + " / " + field,
                    )
            for category, value in row.get("overrides", {}).items():
                require(
                    actual.get("overrides", {}).get(category) == value,
                    "VALIDATION_FAILED",
                    "Line-function override did not read back: " + row["id"] + " / " + category,
                )
        for identifier in patch_functions.get("remove", []):
            require(identifier not in by_id, "VALIDATION_FAILED", "Line-function removal did not persist")


def _apply_patch(scene, wrapped, scope_group_ids, scope, patch, job):
    from toon_workflow.toon_viewport_preview import (
        file_authoring,
        group_lifecycle,
        group_ui,
        line_functions,
        selector_bindings,
    )

    document = wrapped["document"]
    recipe = wrapped["recipe"]
    _, label_to_id = _group_maps(wrapped)

    def group_id(value):
        if value in {group["id"] for group in document["request"]["boundary"]["groups"]}:
            return value
        gid = label_to_id.get(value)
        require(gid is not None, "CONFLICT", "Unknown or ambiguous group: " + value)
        return gid

    for row in patch.get("groups", []):
        gid = group_id(row["group"])
        require(gid in scope_group_ids, "CONFLICT", "Group is outside scope.groups: " + row["group"])
        controls = wrapped.setdefault("scene_controls", {}).setdefault("groups", [])
        control = next((item for item in controls if item.get("name") == gid), None)
        require(control is not None, "CONFLICT", "Missing portable group control: " + gid)
        for field in ("width_scale", "opacity", "enabled"):
            if field in row:
                control[field] = row[field]
        row["_group_id"] = gid

    owned = group_lifecycle.owned(document)
    for row in patch.get("brushes", []):
        gid = group_id(row["group"])
        require(gid in scope_group_ids, "CONFLICT", "Group is outside scope.groups: " + row["group"])
        require(gid in owned, "UNSUPPORTED", "Brush-level edits require an owned autonomous group: " + row["group"])
        actual = row.get("actual")
        if actual is None:
            actual = group_lifecycle.identity(gid, row["channel"], row["role"])
        require(actual in owned[gid]["brushes"], "CONFLICT", "Unknown owned brush: " + str(actual))
        values = {
            key: row[key]
            for key in ("native_size", "color", "blend_amount")
            if key in row
        }
        document, _plan, _bindings, _report = group_lifecycle.migrate(
            document, recipe, {"action": "BRUSH", "group": gid, "actual": actual, "values": values}
        )
        row["_group_id"] = gid
        row["_actual"] = actual

    selector_rows = []
    existing_selectors, _ = selector_bindings.records(document)
    for row in patch.get("selectors", []):
        gid = group_id(row["group"])
        require(gid in scope_group_ids, "CONFLICT", "Group is outside scope.groups: " + row["group"])
        if row.pop("initialize_empty", False):
            require(gid not in existing_selectors, "CONFLICT", "This group already has a saved selector")
            row.update(enabled=True, objects=[], collections=[], materials=[])
        current = existing_selectors.get(gid, {})
        for kind in ("objects", "collections", "materials"):
            if kind in row:
                removed = [ref for ref in current.get(kind, []) if ref not in row[kind]]
                require(all(ref in scope[kind] for ref in removed), "CONFLICT",
                        "Replacing this selector would remove an undeclared " + kind + " target")
        selector_rows.append({**{k: v for k, v in row.items() if not k.startswith("_")}, "group": gid})
        row["_group_id"] = gid
    selector_changes = {"groups":selector_rows}
    if "selector_order" in patch:
        known = {row["id"] for row in document["request"]["boundary"]["groups"]}
        require(set(scope_group_ids) == known, "CONFLICT", "Changing selector priority requires all groups in scope")
        requested = patch["selector_order"]
        order = None if requested is None else [group_id(key) for key in requested]
        require(order is None or (len(order) == len(known) and set(order) == known),
                "INVALID_REQUEST", "Selector priority requires every group exactly once")
        selector_changes["order"] = order
    if selector_rows or "order" in selector_changes:
        document, _plan, _bindings, _report = selector_bindings.migrate(
            document, recipe, selector_changes
        )
        if "order" in selector_changes:
            require(selector_bindings.records(document)[1] == selector_changes["order"],
                    "VALIDATION_FAILED", "Native selector priority differs")

    wrapped["document"] = document
    wrapped["recipe"] = recipe
    used = _used_materials(scene, wrapped, scope_group_ids)
    scope_materials = {item["name"] for item in scope["materials"]}
    if "line_functions" in patch:
        function_patch = patch["line_functions"]
        existing = {
            row["id"]: row
            for row in (document["request"].get("line_functions") or {}).get("containers", [])
        }
        updates = []
        for request_row in function_patch.get("containers", []):
            row = copy.deepcopy(existing.get(request_row["id"])) or line_functions.default(request_row["id"])
            if request_row["id"] in existing:
                names = {item["name"] for item in row.get("materials", [])}
                require(
                    names <= scope_materials and all(ref["library"] is None for ref in row.get("materials", [])),
                    "CONFLICT",
                    "Existing line-function container is outside scope.materials: " + request_row["id"],
                )
                for field in ("draw_objects", "draw_materials", "mask_objects", "mask_materials"):
                    require(
                        not row.get(field),
                        "UNSUPPORTED",
                        "Cannot update a container with existing " + field + " targets in v1",
                    )
            if "materials" in request_row:
                row["materials"] = copy.deepcopy(request_row["materials"])
            require(row.get("materials"), "INVALID_REQUEST", "Line-function container requires materials")
            names = {item["name"] for item in row["materials"]}
            require(
                names <= scope_materials,
                "CONFLICT",
                "Line-function material is outside scope.materials: " + request_row["id"],
            )
            require(
                names <= used,
                "CONFLICT",
                "Line-function material is not selected by scope groups: " + request_row["id"],
            )
            for gate in (
                "disable_intersection",
                "draw_hidden_lines",
                "draw_hidden_lines_of_targets",
                "mask_hidden_lines_of_targets",
            ):
                if gate in request_row:
                    row[gate] = request_row[gate]
            for field in ("draw_objects", "draw_materials", "mask_objects", "mask_materials"):
                if field in request_row:
                    row[field] = []
            for category, value in request_row.get("overrides", {}).items():
                row["overrides"][category] = copy.deepcopy(value)
            updates.append(row)
        for identifier in function_patch.get("remove", []):
            row = existing.get(identifier)
            require(row is not None, "CONFLICT", "Unknown line-function container: " + identifier)
            names = {item["name"] for item in row.get("materials", [])}
            require(
                names and names <= scope_materials and names <= used
                and all(ref["library"] is None for ref in row.get("materials", []))
                and not any(row.get(field) for field in ("draw_objects", "draw_materials", "mask_objects", "mask_materials")),
                "CONFLICT",
                "Cannot remove an out-of-scope line-function container: " + identifier,
            )
        if updates or function_patch.get("remove"):
            document, _plan, _bindings, _report = line_functions.migrate(
                document,
                recipe,
                {
                    "containers": updates,
                    "remove": function_patch.get("remove", []),
                },
            )
    wrapped["document"] = document
    wrapped["recipe"] = recipe

    style_path = job / "patched-style.json"
    require(not style_path.exists(), "CONFLICT", "Internal style path already exists")
    style_path.write_text(
        json.dumps(wrapped, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf8",
    )
    settings = scene.toon_viewport_preview
    previous = group_ui.snapshot(settings)
    file_authoring.import_style(scene, style_path, mode="REPLACE")
    current = group_ui.snapshot(settings)
    by_name = {row["name"]: row for row in current["groups"]}
    saved_order = [row["name"] for row in previous["groups"]]
    require(set(by_name) == set(saved_order) and current["elements"].keys() == previous["elements"].keys(),
            "VALIDATION_FAILED", "Style edit changed active group or render element identities")
    # Native import follows draw order; edit must keep the original RNA order
    # and selectors, including for other tools with index-bound group channels.
    ordered = [by_name[gid] for gid in saved_order]
    if current["groups"] != ordered or current["elements"] != previous["elements"]:
        group_ui.apply(settings, {**current, "groups": ordered, "elements": previous["elements"]})


def _load_preview(bpy, product, edition):
    require(
        tuple(bpy.app.version) == tuple(product["host"]),
        "UNSUPPORTED",
        "Edition " + edition + " requires Blender " + ".".join(map(str, product["host"])),
    )
    bundle = Path(product["directory"]).resolve()
    require(bundle.is_dir(), "NOT_FOUND", "Toon package directory is missing")
    manifest_path = bundle / "bundle.json"
    manifest = read_json(manifest_path)
    require(digest(manifest_path) == product["manifest_sha256"], "CONFLICT", "bundle.json SHA differs")
    require(manifest.get("version") == edition, "CONFLICT", "Toon package edition differs")
    spec = importlib.util.spec_from_file_location(
        "toon_workflow",
        bundle / "__init__.py",
        submodule_search_locations=[str(bundle)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    package.register()
    preview = importlib.import_module("toon_workflow.toon_viewport_preview")
    require(
        Path(preview.__file__).resolve().is_relative_to(bundle),
        "CONFLICT",
        "Loaded viewport package is outside the selected bundle",
    )
    return preview


def saved_element_state(settings):
    from toon_workflow.toon_viewport_preview import addon

    values = addon.element_configuration(settings)
    for row, item in zip(values, settings.elements):
        row["group_filters"] = [{"id": group.name, "label": group.label, "enabled": group.enabled}
                                for group in item.group_filters]
        row["saved_depth"] = {"z_min": item.depth_min, "z_max": item.depth_max}
    active = settings.active_element
    return {"elements": values, "element_budget_mb": settings.element_budget_mb,
            "active_element": values[active]["id"] if 0 <= active < len(values) else None}


def _element_protection(bpy, scene, wrapped):
    from inspection import custom_value

    authored = copy.deepcopy(wrapped)
    authored["scene_controls"].pop("elements", None)
    settings = custom_value(scene.get("toon_viewport_preview")) or {}
    for field in ("elements", "element_budget_mb", "active_element"):
        settings.pop(field, None)
    return fingerprint([authored, settings, _bindings(bpy), _dependencies(bpy)])


def set_element_values(addon, settings, item, patch):
    current = next(row for row in addon.element_configuration(settings) if row["id"] == item.name)
    wanted = {**current, **patch}
    if "background" in patch:
        require(isinstance(patch["background"], dict), "INVALID_REQUEST", "background must be an object")
        wanted["background"] = {**current["background"], **patch["background"]}
    if wanted["type"] == "DEPTH":
        depth = patch.get("depth", {})
        require(isinstance(depth, dict), "INVALID_REQUEST", "depth must be an object")
        wanted["depth"] = {"z_min": item.depth_min, "z_max": item.depth_max, **depth}
    elif "depth" not in patch:
        wanted.pop("depth", None)
    native = addon.render_elements
    normalized = native.normalize([wanted], style_mode=settings.style_mode,
                                  group_ids=[group.name for group in settings.groups])["elements"][0]
    require(all(value >= 0 for value in normalized["background"]["rgba"]), "INVALID_REQUEST",
            "Saved background channels must be nonnegative")
    direct_fields = ("label", "visible_lines_on", "hidden_lines_on", "line_set_ids",
                     *(name + "_on" for name in native.CATEGORIES))
    for key in direct_fields:
        if key in patch:
            setattr(item, key, normalized[key])
    if "type" in patch:
        item.element_type = normalized["type"]
    if "depth" in patch:
        item.depth_min, item.depth_max = normalized["depth"]["z_min"], normalized["depth"]["z_max"]
    if "group_uids" in patch:
        item.use_group_filter = normalized["group_uids"] is not None
        if item.use_group_filter:
            addon._sync_element_groups(item, settings)
            for group in item.group_filters:
                group.enabled = group.name in normalized["group_uids"]
    if "background" in patch:
        item.background_enabled = normalized["background"]["enabled"]
        item.background_color = normalized["background"]["rgba"]

    actual = next(row for row in addon.element_configuration(settings) if row["id"] == item.name)
    for key in (*direct_fields, "type"):
        require(actual[key] == normalized[key], "VALIDATION_FAILED", "Saved element field did not replay: " + key)
    groups = normalized["group_uids"]
    actual_groups = actual["group_uids"]
    require(actual_groups == groups or (groups is not None and actual_groups is not None
            and set(actual_groups) == set(groups)), "VALIDATION_FAILED", "Saved group filter did not replay")
    require(actual["background"]["enabled"] == normalized["background"]["enabled"]
            and all(math.isclose(a, b, rel_tol=2e-6, abs_tol=1e-7)
                    for a, b in zip(actual["background"]["rgba"], normalized["background"]["rgba"])),
            "VALIDATION_FAILED", "Saved element background did not replay")
    if "depth" in normalized:
        require(all(math.isclose(value, normalized["depth"][key], rel_tol=2e-6, abs_tol=1e-7)
                    for key, value in actual["depth"].items()), "VALIDATION_FAILED", "Saved depth did not replay")


def apply_element_step(bpy, addon, settings, step):
    action, uid = step["action"], step["id"]
    before = saved_element_state(settings)
    order = [item["id"] for item in before["elements"]]
    if action == "ADD":
        require(uid not in order, "CONFLICT", "Element already exists: " + uid)
        changed_id = uid
    else:
        require(uid in order, "NOT_FOUND", "Saved element does not exist: " + uid)
        index = order.index(uid)
        settings.active_element = index
        changed_id = step.get("new_id", uid)
        if action == "COPY":
            require(changed_id not in order, "CONFLICT", "Element already exists: " + changed_id)
        if action in {"UP", "DOWN"}:
            target = index + (-1 if action == "UP" else 1)
            require(0 <= target < len(order), "CONFLICT", "Cannot move an element past the list boundary")
            order[index], order[target] = order[target], order[index]
    if action in {"ADD", "COPY"}:
        require(len(settings.elements) < addon.render_elements.MAX_ELEMENTS, "CONFLICT", "Saved element limit reached")
        order.append(changed_id)
    if action == "REMOVE":
        order.remove(uid)
    if action not in {"UPDATE", "SELECT"}:
        require(bpy.ops.toon_viewport.manage_element(action=action) == {"FINISHED"},
                "VALIDATION_FAILED", "Native element action failed: " + action)
    if action in {"ADD", "COPY"}:
        settings.elements[-1].name = changed_id
    if "values" in step:
        set_element_values(addon, settings, settings.elements.get(changed_id), step["values"])

    if action != "SELECT" and before["active_element"] in order:
        settings.active_element = order.index(before["active_element"])
    after = saved_element_state(settings)
    if action == "SELECT":
        require(after == {**before, "active_element": uid}, "VALIDATION_FAILED",
                "Selecting an element did not preserve its list and budget")
    after_by_id = {row["id"]: row for row in after["elements"]}
    require(list(after_by_id) == order, "VALIDATION_FAILED", "Native element order differs from the request")
    mutable = changed_id if action in {"UPDATE", "SYNC_GROUPS", "REMOVE"} else None
    require(all(after_by_id.get(row["id"]) == row for row in before["elements"] if row["id"] != mutable),
            "VALIDATION_FAILED", "Native action changed an unselected element")
    if action == "COPY":
        copied = after_by_id[changed_id]
        initial = next(row for row in before["elements"] if row["id"] == uid)
        require(copied == {**initial, "id": changed_id, "label": copied["label"]},
                "VALIDATION_FAILED", "Native copy changed the element settings")
    return {"action": action, "id": uid, "result_id": changed_id if changed_id in after_by_id else None,
            "order": order, "changed": before != after}


def _save_element_or_line_candidate(bpy, request):
    from inspection import all_ids, identity

    retained = []
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
            item.use_fake_user = True
            retained.append(identity(item))
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=request["stage"], copy=True, relative_remap=True,
                                      check_existing=False) == {"FINISHED"}, "IO_ERROR", "Could not save candidate")
    require(digest(request["file"]) == request["expected_sha256"], "CONFLICT", "Source changed during authoring")
    return retained


def element_work(bpy, context, request, wrapped, author):
    from toon_workflow.toon_viewport_preview import addon, file_authoring

    scene = context.scene
    settings = scene.toon_viewport_preview
    protected = _element_protection(bpy, scene, wrapped)
    result = {"operation": request["operation"], "product": request["product"], "context": request["context"],
              "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
              "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if request["operation"] == "inspect-elements":
        state = saved_element_state(settings)
        if "verify_scene_sha256" in request:
            require(protected == request["verify_scene_sha256"] and fingerprint(state) == request["verify_state_sha256"],
                    "VALIDATION_FAILED", "Cold reopen altered saved elements or protected author state")
        return {**result, "author_sha256": author, "state_sha256": fingerprint(state), "saved_elements": state}

    require(scene.library is None and scene.override_library is None and scene.is_editable, "CONFLICT",
            "Element editing requires a local editable scene")
    events = [apply_element_step(bpy, addon, settings, step) for step in request["element_actions"]]
    if "element_budget_mb" in request:
        settings.element_budget_mb = request["element_budget_mb"]
        require(settings.element_budget_mb == request["element_budget_mb"], "VALIDATION_FAILED", "Element budget did not persist")
    rows = addon.element_configuration(settings)
    if rows:
        addon.render_elements.normalize(rows, style_mode="PRESET", group_ids=[group.name for group in settings.groups])
    require(_element_protection(bpy, scene, file_authoring.read_scene(scene)) == protected, "VALIDATION_FAILED",
            "Element editing changed protected line, scene or dependency state")
    state = saved_element_state(settings)
    after = _author_sha256(scene)
    retained = _save_element_or_line_candidate(bpy, request)
    return {**result, "author_before_sha256": author, "author_after_sha256": after,
            "state_sha256": fingerprint(state), "scene_sha256": protected, "candidate_sha256": digest(request["stage"]),
            "saved_elements": state, "element_events": events, "retained_orphans": retained,
            "protected": {"status": "passed", "scene_sha256": protected, "changed_sections": []}}


def render_camera(bpy, context, request, job, author):
    from toon_workflow.toon_viewport_preview import addon, author_cli, output_scope, render_output
    from toon_workflow.toon_viewport_preview import runtime_preferences, sequence_output, sequence_protocol

    camera = context.scene.camera
    require(camera is not None and camera.type == "CAMERA", "NOT_FOUND", "Saved scene requires a render camera")
    folder = job/"frames"
    folder.mkdir()
    scene = context.scene
    settings = scene.toon_viewport_preview
    sequence = request.get("saved_sequence", False)
    saved = settings.sequence_elements if sequence else request.get("saved_elements", False)
    frames = request.get("frames")
    if sequence:
        frames = addon._sequence_frames(scene, settings)
        require(0 < len(frames) <= 96, "INVALID_REQUEST", "Saved sequence requires 1..96 frames per job")
    suffix = ".exr" if saved or "elements" in request or (sequence and settings.sequence_format == "OPEN_EXR") else ".png"
    target = folder/("line"+suffix)
    owner = sequence_output if frames is not None else render_output
    if saved or sequence:
        elements = addon.element_configuration(settings) if saved else None
        require(not saved or elements, "NOT_FOUND", "The scene has no saved render elements")
        with output_scope.scoped(scene, context.view_layer.name):
            if frames is not None:
                submitted = addon.request_sequence_output(target, context, frames=frames, elements=elements)
            else:
                submitted = addon.request_render_elements(target, context, elements=elements)
        submitted = runtime_preferences.watch(context.scene, owner, submitted)
    else:
        submitted = author_cli.execute(dict(action="OUTPUT", path=str(target), frames=frames,
                                            elements=request.get("elements"), extension=suffix), context)
    require(submitted.get("id"), "WORKER_FAILED", submitted.get("error") or "Line output failed during setup")
    while True:
        owner.poll()
        state = next((j for j in owner.jobs() if j["id"] == submitted["id"]), None)
        require(state is not None, "WORKER_FAILED", "Line output job disappeared")
        if state["status"] != "QUEUED":
            break
        time.sleep(.15)
    require(state["status"] == "COMPLETE", "WORKER_FAILED", state.get("message") or state["status"])
    files = []
    for frame in frames if frames is not None else [context.scene.frame_current]:
        path = folder/sequence_protocol.frame_name("line",frame,suffix) if frames is not None else target
        require(path.is_file() and path.stat().st_size > 0, "VALIDATION_FAILED", f"Missing line output for frame {frame}")
        files.append(dict(file=str(path), frame=frame, sha256=digest(path), bytes=path.stat().st_size))
    require(digest(request["file"]) == request["expected_sha256"], "CONFLICT", "Source changed during line rendering")
    result = dict(operation="render-camera", camera=camera.name_full, frames=files,
                  native_job=submitted["id"], author_sha256=author, context=request["context"],
                  source=dict(file=request["file"], sha256=request["expected_sha256"], unchanged=True),
                  runtime=dict(blender=bpy.app.version_string, build=bpy.app.build_hash.decode(), pid=os.getpid()))
    if saved:
        result.update(saved_elements=elements, element_budget_mb=settings.element_budget_mb)
    if sequence:
        result["saved_sequence"] = {field: getattr(settings, field) for field in sorted(scene_controls.SEQUENCE)}
    return result


def work(request, job):
    import bpy

    _load_preview(bpy, request["product"], request["edition"])
    from toon_workflow.toon_viewport_preview import file_authoring

    source_before = digest(request["file"])
    require(source_before == request["expected_sha256"], "CONFLICT", "Source .blend SHA differs")
    context = (corrections.open_upgrade(bpy, request) if request["operation"] == corrections.UPGRADE
               else open_saved_context(bpy, request))
    scene = context.scene
    if request["operation"] in templates.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return templates.work(bpy, context, request, job, author, save_candidate=_save_element_or_line_candidate,
                              author_sha=_author_sha256, dependencies=lambda: _dependencies(bpy))
    if request["operation"] in style_sets.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return style_sets.work(bpy, context, request, job, author, save_candidate=_save_element_or_line_candidate,
                               author_sha=_author_sha256, dependencies=lambda: _dependencies(bpy),
                               read_bindings=lambda: _bindings(bpy))
    if request["operation"] in style_families.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return style_families.work(bpy, context, request, job, author, save_candidate=_save_element_or_line_candidate,
                                   author_sha=_author_sha256, dependencies=lambda: _dependencies(bpy),
                                   read_bindings=lambda: _bindings(bpy))
    if request["operation"] in file_output.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        if request["operation"] == file_output.RENDER:
            return file_output.render(bpy, context, request, job, author)
        return file_output.edit_work(bpy, context, request, author, save_candidate=_save_element_or_line_candidate,
                                     author_sha=_author_sha256, read_bindings=lambda: _bindings(bpy),
                                     dependencies=lambda: _dependencies(bpy))
    if request["operation"] in author_graph.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return author_graph.work(bpy, context, request, author, author_sha=_author_sha256,
                                 save_candidate=_save_element_or_line_candidate, read_bindings=lambda: _bindings(bpy),
                                 dependencies=lambda: _dependencies(bpy))
    if request["operation"] in author_file.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return author_file.work(bpy, context, request, job, author, author_sha=_author_sha256,
                                save_candidate=_save_element_or_line_candidate, read_bindings=lambda: _bindings(bpy),
                                dependencies=lambda: _dependencies(bpy))
    if request["operation"] in corrections.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return corrections.work(bpy, context, request, author, author_sha=_author_sha256,
                                 save_candidate=_save_element_or_line_candidate, read_bindings=lambda: _bindings(bpy),
                                 dependencies=lambda: _dependencies(bpy))
    if request["operation"] in workbench_jobs.OPERATIONS:
        author = _author_sha256(scene)
        require(request.get("expected_author_sha256", author) == author, "CONFLICT", "Current author SHA differs: " + author)
        return workbench_jobs.work(bpy, context, request, author, author_sha=_author_sha256,
                                   save_candidate=_save_element_or_line_candidate, read_bindings=lambda: _bindings(bpy),
                                   dependencies=lambda: _dependencies(bpy))
    author = _author_sha256(scene)
    if request.get("expected_author_sha256") is not None:
        require(
            author == request["expected_author_sha256"],
            "CONFLICT",
            "Current author SHA differs: " + author,
        )
    settings = scene.toon_viewport_preview
    if request["operation"] in style_pair.OPERATIONS:
        return style_pair.work(bpy,context,request,author,bindings=lambda: _bindings(bpy),
            dependencies=lambda: _dependencies(bpy),save_candidate=_save_element_or_line_candidate,author_sha=_author_sha256)
    if request["operation"] in (scene_controls.READ, scene_controls.WRITE):
        return scene_controls.work(bpy, context, request, author, bindings=lambda: _bindings(bpy),
            dependencies=lambda: _dependencies(bpy), save_candidate=_save_element_or_line_candidate, author_sha=_author_sha256)
    if request["operation"] == "render-camera":
        require(settings.style_mode == "BASIC" or settings.preset_text is not None,
                "UNSUPPORTED", "PRESET rendering requires a saved Pencil document")
        return render_camera(bpy, context, request, job, author)
    require(settings.preset_text is not None, "UNSUPPORTED", "Scene has no saved PRESET style")
    require(settings.style_mode == "PRESET", "UNSUPPORTED", "This operation requires PRESET mode")
    wrapped = file_authoring.read_scene(scene)
    if request["operation"] in (visibility.READ, visibility.WRITE):
        return visibility.work(bpy, context, request, wrapped, author, bindings=lambda: _bindings(bpy),
            dependencies=lambda: _dependencies(bpy), save_candidate=_save_element_or_line_candidate, author_sha=_author_sha256)
    if request["operation"] in {"manage-elements", "inspect-elements"}:
        return element_work(bpy, context, request, wrapped, author)
    if request["operation"] in (hierarchy.READ, hierarchy.WRITE):
        return hierarchy.work(
            bpy, context, request, wrapped, author, scene_dependencies=lambda: _dependencies(bpy),
            saved_elements=saved_element_state, save_candidate=_save_element_or_line_candidate, author_sha=_author_sha256,
        )
    if request["operation"] in (tracks.READ, tracks.WRITE):
        return tracks.work(
            bpy, context, request, wrapped, author, dependencies=lambda: _dependencies(bpy),
            save_candidate=_save_element_or_line_candidate, author_sha=_author_sha256,
        )
    if request["operation"] in (groups.READ, groups.WRITE):
        return groups.work(
            bpy, context, request, wrapped, author, save_candidate=_save_element_or_line_candidate,
            saved_elements=saved_element_state, author_sha=_author_sha256,
            protect_subset=lambda result, gids, targets: _protected_subset(bpy, result, gids, targets),
        )
    if request["operation"] in (edges.READ, edges.WRITE):
        return edges.work(
            bpy, context, request, wrapped, author, saved_elements=saved_element_state,
            protect_subset=lambda result, gids, targets: _protected_subset(bpy, result, gids, targets),
            save_candidate=_save_element_or_line_candidate, author_sha=_author_sha256,
        )
    if request["operation"] in (ink.READ, ink.WRITE):
        return ink.work(
            bpy, context, request, wrapped, author, dependencies=lambda: _dependencies(bpy),
            saved_elements=saved_element_state, save_candidate=_save_element_or_line_candidate,
            author_sha=_author_sha256,
        )
    scope_ids, id_to_label = _scope_group_ids(wrapped, request["scope"])
    scope = _resolve_refs(scene, request["scope"])
    used = _used_materials(scene, wrapped, scope_ids)
    state_sha = fingerprint(_state_subset(wrapped))
    if request["operation"] == "inspect":
        scene_hash = fingerprint([_bindings(bpy), _dependencies(bpy)])
        if "verify_scene_sha256" in request:
            require(scene_hash == request["verify_scene_sha256"], "VALIDATION_FAILED",
                    "Object bindings or external data references changed after cold reopen")
        if request.get("verify_state_sha256") is not None:
            require(
                state_sha == request["verify_state_sha256"],
                "VALIDATION_FAILED",
                "Cold state digest differs from the authored candidate",
            )
        source_after = digest(request["file"])
        require(source_after == source_before, "CONFLICT", "Source .blend changed during inspection")
        return {
            "operation": "inspect",
            "source": {"file": request["file"], "sha256": source_before, "unchanged": True},
            "product": request["product"],
            "context": request["context"],
            "scope": {
                "groups": [
                    {"group": gid, "label": id_to_label.get(gid, gid)} for gid in scope_ids
                ],
                "objects": scope["objects"],
                "collections": scope["collections"],
                "materials": scope["materials"],
            },
            "author_sha256": author,
            "state_sha256": state_sha,
            "scene_sha256": scene_hash,
            "saved_elements": saved_element_state(settings),
            "summary": _summary(
                wrapped,
                scope_ids,
                id_to_label,
                used,
                {item["name"] for item in scope["materials"]},
            ),
            "runtime": {
                "blender": bpy.app.version_string,
                "build": bpy.app.build_hash.decode(),
                "pid": os.getpid(),
            },
        }

    require(request.get("expected_author_sha256") is not None, "INVALID_REQUEST", "edit requires author SHA")
    before = copy.deepcopy(wrapped)
    before_protected = _protected_subset(
        bpy,
        wrapped,
        scope_ids,
        {
            row["id"]
            for row in (request["patch"].get("line_functions", {}).get("containers", []))
        }
        | set(request["patch"].get("line_functions", {}).get("remove", [])),
    )
    _apply_patch(scene, wrapped, scope_ids, scope, request["patch"], job)
    after = file_authoring.read_scene(scene)
    _verify_changes(before, after, request["patch"])
    after_author = _author_sha256(scene)
    require(after_author != author, "CONFLICT", "Patch is a no-op; author SHA did not change")
    after_protected = _protected_subset(
        bpy,
        after,
        scope_ids,
        {
            row["id"]
            for row in (request["patch"].get("line_functions", {}).get("containers", []))
        }
        | set(request["patch"].get("line_functions", {}).get("remove", [])),
    )
    changed_sections = _changed_sections(before_protected, after_protected)
    require(
        not changed_sections,
        "VALIDATION_FAILED",
        "Protected state changed: " + ", ".join(changed_sections),
    )
    after_state = fingerprint(_state_subset(after))
    require(after_state != state_sha, "CONFLICT", "Patch is a no-op; state digest did not change")
    from inspection import all_ids, identity
    retained = []
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
            item.use_fake_user = True
            retained.append(identity(item))
    scene_hash = fingerprint([_bindings(bpy), _dependencies(bpy)])
    bpy.context.preferences.filepaths.save_version = 0
    require(
        bpy.ops.wm.save_as_mainfile(
            filepath=request["stage"],
            copy=True,
            relative_remap=True,
            check_existing=False,
        )
        == {"FINISHED"},
        "IO_ERROR",
        "Could not save the viewport-lines candidate",
    )
    source_after = digest(request["file"])
    require(source_after == source_before, "CONFLICT", "Source .blend changed during authoring")
    return {
        "operation": "edit",
        "source": {"file": request["file"], "sha256": source_before, "unchanged": True},
        "product": request["product"],
        "context": request["context"],
        "scope": {
            "groups": [
                {"group": gid, "label": id_to_label.get(gid, gid)} for gid in scope_ids
            ],
            "objects": scope["objects"],
            "collections": scope["collections"],
            "materials": scope["materials"],
        },
        "author_before_sha256": author,
        "author_after_sha256": after_author,
        "state_sha256": after_state,
        "candidate_sha256": digest(request["stage"]),
        "scene_sha256": scene_hash,
        "retained_orphans": retained,
        "changed": {
            "groups": [row["group"] for row in request["patch"].get("groups", [])],
            "brushes": [
                {"group": row["group"], "actual": row.get("_actual")}
                for row in request["patch"].get("brushes", [])
            ],
            "selectors": [row["group"] for row in request["patch"].get("selectors", [])],
            "line_functions": sorted(
                {
                    row["id"]
                    for row in request["patch"].get("line_functions", {}).get("containers", [])
                }
                | set(request["patch"].get("line_functions", {}).get("remove", []))
            ),
        },
        "protected": {
            "status": "passed",
            "non_target_groups_and_selectors": "unchanged",
            "non_target_line_functions": "unchanged",
            "line_hierarchy_and_sources": "unchanged",
            "object_material_bindings_and_dependencies": "unchanged",
        },
        "runtime": {
            "blender": bpy.app.version_string,
            "build": bpy.app.build_hash.decode(),
            "pid": os.getpid(),
        },
    }


def worker(phase, params_path):
    job = Path(params_path).parent
    try:
        request_document = read_json(params_path)
        require(request_document.get("schema_version") == SCHEMA, "INVALID_REQUEST", "Job schema differs")
        require(request_document.get("command") == COMMAND, "INVALID_REQUEST", "Job command differs")
        data = work(request_document["params"], job)
        atomic_json(job / (phase + "-result.json"), {"ok": True, "data": data})
    except Exception as exc:
        atomic_json(
            job / (phase + "-result.json"),
            {"ok": False, "error": error_payload(exc, "VALIDATION_FAILED")},
        )


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_path = job / "result.json"
    artifacts = {"job_directory": str(job), "result": str(result_path)}
    data = error = None

    def checkpoint():
        if (job / "cancel.request").exists():
            raise Failure("CANCELLED", "Viewport-lines operation cancelled before publication")
        require(time.monotonic() < deadline, "TIMEOUT", "Viewport-lines operation exceeded its timeout")

    def status(state, phase):
        atomic_json(
            job / "status.json",
            {
                "job_id": job.name,
                "state": state,
                "phase": phase,
                "pid": os.getpid(),
                "heartbeat_at": time.time(),
            },
        )

    status("running", "verify_inputs")
    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            blender = canonical(args.blender)
            output = None
            stage = None
            if request["operation"] in MUTATING:
                parent = canonical(args.output.parent)
                for folder in reversed((parent, *parent.parents)):
                    if folder != Path(folder.anchor):
                        guards.enter_context(FileGuard(folder, directory=True))
                output = parent / args.output.name
                stage = parent / (".toon-viewport-" + uuid.uuid4().hex + ".blend")
                guards.enter_context(PathLocks([output, stage]))
                require(not os.path.lexists(output), "CONFLICT", "Output already exists")
                require(not os.path.lexists(stage), "CONFLICT", "Stage path already exists")
                descriptor = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                guards.callback(stage.unlink, missing_ok=True)
                request["stage"] = str(stage)
            elif request["operation"] == file_output.RENDER:
                output = file_output.reserve_output(args.output, guards)
            atomic_json(
                job / "request.json",
                {"schema_version": SCHEMA, "command": COMMAND, "params": request},
            )
            if request["operation"] in MUTATING:
                atomic_json(job / "author-request.json", {"schema_version": SCHEMA, "command": COMMAND, "params": request})
                status("running", "author")
                result = run_worker(blender, job, "author", deadline, checkpoint, status, entry=ENTRY, private_temp=True)
                require(
                    result["source"]["sha256"] == request["expected_sha256"],
                    "CONFLICT",
                    "Author worker source SHA differs",
                )
                stage_sha = result["candidate_sha256"]
                with FileGuard(stage) as stage_guard:
                    require(stage_guard.sha256(checkpoint) == stage_sha, "CONFLICT", "Candidate stage SHA differs")
                    cold = {
                        "operation": REOPEN[request["operation"]],
                        "edition": request["edition"],
                        "file": str(stage),
                        "expected_sha256": stage_sha,
                        "expected_author_sha256": result["author_after_sha256"],
                        "context": request["context"],
                        "product": request["product"],
                        "verify_state_sha256": result["state_sha256"],
                        "verify_scene_sha256": result["scene_sha256"],
                    }
                    if "scope" in request:
                        cold["scope"] = request["scope"]
                    atomic_json(
                        job / "request.json",
                        {"schema_version": SCHEMA, "command": COMMAND, "params": cold},
                    )
                    atomic_json(job / "reopen-request.json", {"schema_version": SCHEMA, "command": COMMAND, "params": cold})
                    status("running", "reopen")
                    reopened = run_worker(blender, job, "reopen", deadline, checkpoint, status, entry=ENTRY, private_temp=True)
                require(
                    reopened["runtime"]["pid"] != result["runtime"]["pid"],
                    "VALIDATION_FAILED",
                    "Independent reopen did not use a new Blender process",
                )
                require(
                    reopened["author_sha256"] == result["author_after_sha256"],
                    "CONFLICT",
                    "Cold author SHA differs from the authored candidate",
                )
                require(
                    reopened["state_sha256"] == result["state_sha256"],
                    "CONFLICT",
                    "Cold state digest differs from the authored candidate",
                )
                require(
                    reopened["source"]["sha256"] == stage_sha,
                    "CONFLICT",
                    "Cold reopen read a different candidate file",
                )
                if request["operation"] == author_file.EDIT and result["event"]["action"] == "STORE_PREVIEW":
                    ref = result["event"]["artifact"]
                    preview = job / author_file.ARTIFACTS["PREVIEW"]
                    require(ref["file"] == str(preview) and digest(preview) == ref["sha256"],
                            "CONFLICT", "Packed preview source image changed before publication")
                    artifacts["author_file"] = str(preview)
                with FileGuard(stage, renameable=True) as stage_guard:
                    require(stage_guard.sha256(checkpoint) == stage_sha, "CONFLICT", "Verified candidate changed")
                    checkpoint()
                    stage_guard.rename_new(output)
                result["candidate"] = {"file": str(output), "sha256": stage_sha}
                result["independent_reopen"] = reopened["runtime"]
                result["reopen_author_sha256"] = reopened["author_sha256"]
                result["reopen_state_sha256"] = reopened["state_sha256"]
                result["verification"] = {
                    "status": "passed",
                    "author_sha256": result["author_after_sha256"],
                    "state_sha256": result["state_sha256"],
                    "protected": result["protected"],
                }
                artifacts["candidate"] = str(output)
                artifacts["independent_reopen"] = str(job / "reopen-result.json")
                data = {
                    **result,
                    "source_unchanged": digest(request["file"]) == request["expected_sha256"],
                }
            else:
                phase = request["operation"]
                status("running", phase)
                data = run_worker(blender, job, phase, deadline, checkpoint, status, entry=ENTRY, private_temp=True)
                if phase == file_output.RENDER:
                    manifest = file_output.publish_output(data, job, output, checkpoint, guards)
                    data["manifest"] = manifest
                    artifacts["media_manifest"] = manifest["file"]
                    artifacts["output_directory"] = str(output)
                elif phase == templates.CAPTURE:
                    artifact = job / "line-template.json"
                    require(data["template_file"]["file"] == str(artifact) and digest(artifact) == data["template_file"]["sha256"],
                            "CONFLICT", "Native template export changed after capture")
                    artifacts["template"] = str(artifact)
                elif phase == style_sets.EXPORT:
                    artifact = job / "style-set.json"
                    require(data["package"]["file"] == str(artifact) and digest(artifact) == data["package"]["sha256"],
                            "CONFLICT", "Native Style Set export changed after capture")
                    artifacts["style_set"] = str(artifact)
                elif phase == style_families.EXPORT:
                    artifact = job / "style-family.json"
                    require(data["package"]["file"] == str(artifact) and digest(artifact) == data["package"]["sha256"],
                            "CONFLICT", "Native Style Family export changed after capture")
                    artifacts["style_family"] = str(artifact)
                elif phase == workbench_jobs.PREPARE:
                    for key in ("submission", "workflow_request"):
                        ref = data[key]
                        require(digest(ref["file"]) == ref["sha256"], "CONFLICT", "Native workbench artifact changed after preparation")
                        artifacts[key] = ref["file"]
                elif phase == author_file.EXPORT:
                    ref = data["artifact"]
                    artifact = job / author_file.ARTIFACTS[data["event"]]
                    require(ref["file"] == str(artifact) and digest(artifact) == ref["sha256"],
                            "CONFLICT", "Author artifact changed after export")
                    artifacts["author_file"] = str(artifact)
    except Failure as exc:
        error = error_payload(exc, "VALIDATION_FAILED")
    except ValueError as exc:
        error = error_payload(Failure("INVALID_REQUEST", str(exc)), "INVALID_REQUEST")
    except (OSError, RuntimeError) as exc:
        error = error_payload(exc, "IO_ERROR")
    except Exception as exc:
        error = error_payload(Failure("VALIDATION_FAILED", str(exc)), "VALIDATION_FAILED")
    for phase in ("inspect", "author", "reopen", "render-camera", hierarchy.READ, tracks.READ, groups.READ, edges.READ,
                  ink.READ, templates.READ, templates.CAPTURE, style_sets.READ, style_sets.EXPORT,
                  style_families.READ, style_families.EXPORT, file_output.READ, file_output.RENDER, scene_controls.READ,
                  visibility.READ, style_pair.READ, workbench_jobs.READ, workbench_jobs.PREPARE, workbench_jobs.REFRESH, author_file.READ, author_file.EXPORT,
                  author_file.READ_PICKER):
        for suffix in (".log", "-result.json", "-request.json"):
            path = job / (phase + suffix)
            if path.is_file():
                artifacts[path.name] = str(path)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(result_path, result)
    terminal = {"TIMEOUT": "timed_out", "CANCELLED": "cancelled"}
    status(terminal.get(error["code"], "failed") if error else "succeeded", "done")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, "IO_ERROR"))
    if args and args.human:
        if result["ok"]:
            print(result["artifacts"]["result"])
            if "candidate" in result["artifacts"]:
                print(result["artifacts"]["candidate"])
        else:
            print(result["error"]["message"])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES[result["error"]["code"]]


if __name__ == "__main__":
    if "--background" in sys.argv and "--" in sys.argv:
        values = sys.argv[sys.argv.index("--") + 1 :]
        if len(values) != 2:
            raise SystemExit("Worker expects: PHASE REQUEST_JSON")
        worker(values[0], values[1])
    else:
        raise SystemExit(main())
