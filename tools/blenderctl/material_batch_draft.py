"""Export a committed material batch, open one assignment, and explicitly commit its draft."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import (
    EDITIONS, EDITION_HELP, Parser, canonical, failure, fingerprint, guard_file, name, read_document,
    require, run_operator, select_product,
)
from processes import ProcessTree
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, atomic_json, digest as digest_file, envelope, read_json, timeout_value


WORKER_MARKER = "--material-batch-draft-worker"
FIELDS = {
    "export-batch-draft": set(),
    "edit-batch-panel": {"edit"},
    "edit-batch-assignment": {"assignment_id", "target"},
    "commit-batch-assignment": {"assignment_id", "target", "edit_receipt", "draft"},
}
CONTEXT_KEYS = {"scene", "view_layer", "frame"}
TARGET_KEYS = {"object", "material_slot", "uv_layer"}


def parser():
    cli = Parser(prog="blenderctl material", description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER, help=argparse.SUPPRESS)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    cli.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for flag in ("--human", "--compact", "--full"):
        display.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    actions = cli.add_subparsers(dest="action", required=True, parser_class=Parser)
    descriptions = {
        "export-batch-draft": "Save the committed batch JSON from a saved Object Mode scene.",
        "edit-batch-panel": "Save the edited panel in a new .blend. edit.op: load, capture-context, select-view, "
                            "add-view, delete-view, edit-view, view-objects, preview-settings, "
                            "add-assignment (object), select-assignment, delete-assignment, capture-faces, "
                            "edit-target (assignment_id, target, faces, depends_on). "
                            "load needs manifest {file,expected_sha256}. View operations need view_id; "
                            "edit-view and preview-settings take values. view-objects takes an objects list.",
        "edit-batch-assignment": "Export one assignment as a single-material JSON draft. Keep the CLI result as the edit receipt.",
        "commit-batch-assignment": "Commit the edited JSON's layers and material.name to its original assignment only. "
                                   "Supply the edit_receipt and draft as {file, expected_sha256} descriptors.",
    }
    for command, fields in FIELDS.items():
        context = "Common: file, expected_sha256, edition, context {scene, view_layer, frame}. "
        detail = "Also require " + ", ".join(sorted(fields)) + ". " if fields else ""
        target = "target is {object, material_slot, uv_layer}. " if "target" in fields else ""
        sub = actions.add_parser(command, description=descriptions[command], allow_abbrev=False,
                                 epilog=context + detail + target + descriptions[command])
        sub.add_argument("--params", required=True, type=Path, help="UTF-8 JSON request")
        sub.add_argument("--output", required=True, type=Path, help="New absolute .blend" if command=="edit-batch-panel" else "New absolute JSON path")
    return cli


def read_bound(value, stack):
    require(isinstance(value, dict) and set(value) == {"file", "expected_sha256"} and isinstance(value["file"], str),
            "INVALID_REQUEST", "A JSON input descriptor requires file and expected_sha256")
    return read_document(guard_file(value["file"], stack, value["expected_sha256"]))


def inputs(args, stack):
    request = read_document(guard_file(args.params.absolute(), stack))
    required = {"file", "expected_sha256", "edition", "context"} | FIELDS[args.action]
    require(isinstance(request, dict) and set(request) == required,
            "INVALID_REQUEST", "Expected fields: " + ", ".join(sorted(required)))
    require(isinstance(request["edition"], str) and request["edition"] in EDITIONS, "UNSUPPORTED",
            EDITION_HELP)
    context = request["context"]
    require(isinstance(context, dict) and set(context) == CONTEXT_KEYS
            and name(context["scene"]) and name(context["view_layer"])
            and type(context["frame"]) is int and 1 <= context["frame"] <= 100000,
            "INVALID_REQUEST", "Expected saved scene, view_layer and frame")
    require(isinstance(request["file"], str) and isinstance(request["expected_sha256"], str),
            "INVALID_REQUEST", "Working copy requires file and expected_sha256")
    source = guard_file(request["file"], stack, request["expected_sha256"])
    require(source.suffix.lower() == ".blend", "INVALID_REQUEST", "Expected a saved .blend working copy")
    if "assignment_id" in request:
        target = request["target"]
        require(name(request["assignment_id"]) and isinstance(target, dict) and set(target) == TARGET_KEYS
                and name(target["object"]) and name(target["uv_layer"])
                and type(target["material_slot"]) is int and 0 <= target["material_slot"] <= 63,
                "INVALID_REQUEST", "Expected assignment_id and target {object, material_slot, uv_layer}")
    if args.action == "commit-batch-assignment":
        receipt = read_bound(request.pop("edit_receipt"), stack)
        require(isinstance(receipt, dict) and receipt.get("ok") is True
                and receipt.get("command") == "material.edit-batch-assignment", "CONFLICT", "Expected a successful edit receipt")
        data = receipt.get("data")
        require(isinstance(data, dict) and isinstance(data.get("edit_binding"), dict), "CONFLICT",
                "Edit receipt has no assignment binding")
        request["binding"] = data["edit_binding"]
        request["draft"] = read_bound(request["draft"], stack)
    if args.action == "edit-batch-panel":
        edit = request["edit"]
        shapes = {"load":{"manifest"}, "capture-context":set(), "select-view":{"view_id"}, "add-view":{"view_id"},
                  "delete-view":{"view_id"}, "edit-view":{"view_id","values"}, "view-objects":{"view_id","objects"},
                  "preview-settings":{"values"}, "add-assignment":{"object"},
                  "select-assignment":{"assignment_id"}, "delete-assignment":{"assignment_id"},
                  "capture-faces":{"assignment_id"}, "edit-target":{"assignment_id","target","faces","depends_on"}}
        require(isinstance(edit,dict) and isinstance(edit.get("op"),str) and edit["op"] in shapes
                and set(edit)==shapes[edit["op"]]|{"op"}, "INVALID_REQUEST", "Unexpected batch edit or fields")
        if "manifest" in edit:
            read_bound(edit["manifest"],stack)
        if "view_id" in edit:
            require(name(edit["view_id"]), "INVALID_REQUEST", "Expected a named preview view")
        for key in ("object", "assignment_id"):
            if key in edit:
                require(name(edit[key]), "INVALID_REQUEST", f"Expected a named {key}")
        if edit["op"] == "edit-target":
            target, faces, deps = edit["target"], edit["faces"], edit["depends_on"]
            require(isinstance(target, dict) and set(target) == TARGET_KEYS and name(target["object"])
                    and name(target["uv_layer"]) and type(target["material_slot"]) is int
                    and 0 <= target["material_slot"] <= 63, "INVALID_REQUEST", "Expected object, slot and UV layer")
            require(faces is None or (isinstance(faces, list) and 0 < len(faces) <= 100000
                    and all(type(v) is int and v >= 0 for v in faces) and len(faces) == len(set(faces))),
                    "INVALID_REQUEST", "Expected null or unique nonnegative face indices")
            require(isinstance(deps, list) and len(deps) <= 64 and all(name(v) for v in deps)
                    and len(deps) == len(set(deps)), "INVALID_REQUEST", "Expected unique dependency IDs")
        if "objects" in edit:
            targets = edit["objects"]
            require(isinstance(targets,list) and targets and all(name(v) for v in targets) and len(set(targets))==len(targets),
                    "INVALID_REQUEST","Expected unique named mesh objects")
        if "values" in edit:
            allowed = {"engine","width","height","samples"} if edit["op"]=="preview-settings" else {"azimuth","elevation","lighting","focus","zoom"}
            require(isinstance(edit["values"],dict) and edit["values"] and edit["values"].keys()<=allowed,
                    "INVALID_REQUEST", "Expected a nonempty patch of admitted panel fields")
    return {**request, "file": str(source), "action": args.action}


def open_context(bpy, request):
    require(bpy.ops.wm.open_mainfile(filepath=request["file"], load_ui=True, use_scripts=False) == {"FINISHED"}
            and bpy.data.filepath and Path(bpy.data.filepath).samefile(request["file"]),
            "VALIDATION_FAILED", "Saved working copy could not reopen")
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]), "UNSUPPORTED",
            "Saved .blend and Blender must share major/minor version")
    context = bpy.context
    actual = {"scene": context.scene.name, "view_layer": context.view_layer.name, "frame": context.scene.frame_current}
    require(actual == request["context"] and not context.scene.frame_subframe and context.mode == "OBJECT",
            "CONFLICT", f"Saved Object Mode scene, view layer or frame differs: {actual}")
    return context


def saved_editor(scene, ui):
    fields = {}
    if scene.mw_manifest_json:
        for key in ui.FIELDS:
            value = getattr(scene, key)
            fields[key] = value if isinstance(value, (str, bool, int, float)) else list(value)
    return {
        "present": bool(scene.mw_manifest_json), "assignment_id": scene.mw_batch_edit_id or None,
        "draft_sha256": fingerprint(scene.mw_manifest_json) if scene.mw_manifest_json else None,
        "panel_fields_sha256": fingerprint(fields) if fields else None,
        "preserved_in_source": True, "included_in_batch": False,
    }


def export_batch(bpy, scene, batch, scratch):
    scene.mw_batch_path = str(scratch / "panel-batch.json")
    run_operator(bpy, "batch_save", scene)
    native = read_document(Path(scene.mw_batch_path))
    require(native == batch, "VALIDATION_FAILED", "Native export differs from the committed batch draft")
    return native


def assignment_output(bpy, context, source, request, product):
    from material_workflow_addon import ui, ui_batch
    from material_workflow_addon.batch_contract import assignment_manifest

    scene = context.scene
    matches = [(i, a) for i, a in enumerate(source["assignments"]) if a["id"] == request["assignment_id"]]
    require(len(matches) == 1 and matches[0][1]["target"] == request["target"], "CONFLICT",
            "Saved assignment id, object, material slot or UV layer differs")
    index, row = matches[0]
    obj = context.view_layer.objects.get(row["target"]["object"])
    require(obj is not None and obj.type == "MESH" and not (obj.library or obj.override_library or obj.data.library)
            and row["target"]["uv_layer"] in obj.data.uv_layers, "CONFLICT", "Assignment needs its saved local mesh and UV layer")
    baseline = assignment_manifest(source, row)
    binding = {
        "file": request["file"], "expected_sha256": request["expected_sha256"], "context": request["context"],
        "edition": product["edition"], "implementation": fingerprint(product["files"]),
        "batch_sha256": fingerprint(source), "assignment_sha256": fingerprint(row),
        "assignment_id": row["id"], "material_id": row["material"]["id"], "target": row["target"],
        "single_draft_sha256": fingerprint(baseline),
    }
    committing = request["action"] == "commit-batch-assignment"
    if committing:
        require(request["binding"] == binding, "CONFLICT", "Edit receipt is stale or belongs to a different source, edition or assignment")
    run_operator(bpy, "batch_select", scene, index=index)
    run_operator(bpy, "batch_edit_material", scene)
    require(ui.normalize_manifest(ui._manifest(scene)) == baseline and scene.mw_batch_edit_id == row["id"],
            "VALIDATION_FAILED", "Panel opened a different single-material draft")
    require(ui_batch.get_manifest(scene) == source, "VALIDATION_FAILED", "Loading the editor changed the committed batch")

    if not committing:
        return baseline, {"edit_binding": binding, "consumer": "material commit-batch-assignment"}

    edited = ui.normalize_manifest(request["draft"])
    intent = deepcopy(baseline)
    intent["layers"] = edited["layers"]
    intent["material"]["name"] = edited["material"]["name"]
    require(edited == intent, "CONFLICT", "Only this assignment's layers and material.name can be committed")
    ui._store(scene, edited)
    ui._load_layer(scene)
    # The panel routes active layer values through Blender RNA. Check the native
    # commit against those actual values, then retain portable JSON precision in
    # the handoff so an unrelated field is never rewritten by float32 rounding.
    panel_draft = ui.normalize_manifest(ui._flush(scene))
    expected = deepcopy(source)
    expected["assignments"][index]["layers"] = panel_draft["layers"]
    expected["assignments"][index]["material"]["name"] = panel_draft["material"]["name"]
    run_operator(bpy, "batch_commit_material", scene)
    require(ui_batch.get_manifest(scene) == expected, "VALIDATION_FAILED", "Native commit changed another assignment or batch field")
    expected["assignments"][index]["layers"] = intent["layers"]
    expected["assignments"][index]["material"]["name"] = intent["material"]["name"]
    ui_batch._store(scene, expected)
    batch = ui_batch.get_manifest(scene)
    require(batch == expected, "VALIDATION_FAILED", "Portable commit changed another assignment or batch field")
    return batch, {
        "assignment_id": row["id"], "material_id": row["material"]["id"], "target": row["target"],
        "other_assignments_unchanged": True, "rna_projection_differed": panel_draft != edited,
        "committed_batch_sha256": fingerprint(batch), "consumer": "material batch",
    }


def set_panel_values(scene, values):
    for key, value in values.items():
        field = "mw_batch_" + key
        prop = scene.bl_rna.properties[field]
        if prop.type == "ENUM":
            valid = isinstance(value,str) and value in prop.enum_items.keys()
        else:
            parts = value if prop.is_array else [value]
            valid = (isinstance(parts,list) and len(parts)==(prop.array_length if prop.is_array else 1)
                     and all(type(v) in (int,float) and math.isfinite(v) and prop.hard_min<=v<=prop.hard_max
                             and (prop.type != "INT" or type(v) is int) for v in parts))
        require(valid, "INVALID_REQUEST", f"Invalid {key} for this material panel")
        setattr(scene,field,value)


def panel_dependencies(bpy):
    from inspection import path_value
    from material_procedural_template import external_dependencies

    paths = external_dependencies(bpy)
    # Blender also enumerates its temporary Essentials brush selector. A
    # background save may release that selector even when no brush ID exists.
    builtin_brushes = Path(bpy.utils.system_resource("DATAFILES", path="assets/brushes")).resolve()
    owned = {path_value(raw, owner.library) for owner, values in
             bpy.data.file_path_map(include_libraries=True).items() for raw in values}
    return {p for p in paths if p in owned or not Path(p).is_relative_to(builtin_brushes)}


def assignment_edit(bpy, context, ui_batch, edit):
    scene, op = context.scene, edit["op"]
    if op == "add-assignment":
        obj = context.view_layer.objects.get(edit["object"])
        require(obj is not None and obj.type == "MESH" and obj.data.uv_layers.active,
                "CONFLICT", "Adding an assignment requires a named mesh with an active UV layer")
        # Native ADD consumes pending panel values. Preserve the single editor's
        # saved JSON and association so the new assignment cannot commit it.
        previous_draft = scene.mw_manifest_json
        with context.temp_override(object=obj, active_object=obj):
            run_operator(bpy, "batch_add", scene)
        scene.mw_manifest_json = previous_draft
        return
    rows = ui_batch.get_manifest(scene)["assignments"]
    index = next((i for i, row in enumerate(rows) if row["id"] == edit["assignment_id"]), None)
    require(index is not None, "NOT_FOUND", "Named assignment does not exist")
    if index != scene.mw_batch_index or op == "select-assignment":
        run_operator(bpy, "batch_select", scene, index=index)
    if op == "delete-assignment":
        run_operator(bpy, "batch_delete", scene)
    elif op == "capture-faces":
        obj = context.view_layer.objects.get(rows[index]["target"]["object"])
        require(obj is not None and obj.type == "MESH", "NOT_FOUND", "Assignment target mesh is missing")
        with context.temp_override(object=obj, active_object=obj):
            run_operator(bpy, "batch_faces", scene)
    elif op == "edit-target":
        target, faces = edit["target"], edit["faces"]
        obj = context.view_layer.objects.get(target["object"])
        require(obj is not None and obj.type == "MESH" and target["uv_layer"] in obj.data.uv_layers,
                "NOT_FOUND", "Named mesh or UV layer is missing")
        require(not faces or max(faces) < len(obj.data.polygons), "CONFLICT", "Face index outside target mesh")
        scene.mw_batch_object = obj.name
        scene.mw_batch_slot = target["material_slot"]
        scene.mw_batch_uv = target["uv_layer"]
        scene.mw_batch_faces = json.dumps(faces)
        scene.mw_batch_dependencies = json.dumps(edit["depends_on"])
        run_operator(bpy, "batch_target", scene)


def edit_panel(bpy, context, ui, ui_batch, request):
    from inspection import all_ids, identity
    from material_workflow_addon.contract import image_documents
    from material_procedural_template import slot_state
    scene, edit = context.scene, request["edit"]
    op = edit["op"]
    before_slots = slot_state(bpy)
    before_editor = saved_editor(scene,ui)
    expected_editor = dict(before_editor)
    if op == "load":
        old_id = scene.mw_batch_edit_id
        old = ui_batch.get_manifest(scene) if old_id and scene.mw_batch_json else None
        scene.mw_batch_path = edit["manifest"]["file"]
        run_operator(bpy,"batch_load",scene)
        batch = ui_batch.get_manifest(scene)
        require(batch==ui_batch.normalize_manifest_batch(read_document(Path(scene.mw_batch_path))),
                "VALIDATION_FAILED","Loaded panel differs from the requested manifest")
        if old_id:
            previous = next((row for row in old["assignments"] if row["id"]==old_id),None) if old else None
            current = next((row for row in batch["assignments"] if row["id"]==old_id),None)
            if previous is None or previous!=current:
                scene.mw_batch_edit_id = ""
                expected_editor["assignment_id"] = None
    elif op == "capture-context":
        run_operator(bpy,"batch_context",scene)
    elif op == "preview-settings":
        set_panel_values(scene,edit["values"])
        run_operator(bpy,"batch_preview",scene)
    elif "assignment_id" in edit or op == "add-assignment":
        assignment_edit(bpy, context, ui_batch, edit)
        if op in {"delete-assignment", "edit-target"} and scene.mw_batch_edit_id == edit["assignment_id"]:
            scene.mw_batch_edit_id = ""
            expected_editor["assignment_id"] = None
    else:
        batch = ui_batch.get_manifest(scene)
        views = batch["preview"]["views"]
        ids = [v["id"] for v in views]
        require(ids.count(edit["view_id"])==1,"NOT_FOUND","Named preview view is missing or ambiguous")
        index = ids.index(edit["view_id"])
        if scene.mw_batch_view_index != index or op == "select-view":
            run_operator(bpy,"batch_view",scene,action="SELECT",index=index)
        if op in {"add-view","delete-view"}:
            run_operator(bpy,"batch_view",scene,action="ADD" if op=="add-view" else "DELETE")
        elif op == "edit-view":
            set_panel_values(scene,edit["values"])
            run_operator(bpy,"batch_view",scene,action="COMMIT")
        elif op == "view-objects":
            objects = [context.view_layer.objects.get(n) for n in edit["objects"]]
            require(all(obj is not None and obj.type=="MESH" and obj.name in batch["objects"] for obj in objects),
                    "CONFLICT", "Each preview target must be a saved mesh in the batch and view layer")
            with context.temp_override(selected_objects=objects):
                run_operator(bpy,"batch_view",scene,action="OBJECTS")
    batch = ui_batch.get_manifest(scene)
    require(saved_editor(scene,ui)==expected_editor and slot_state(bpy)==before_slots,
            "VALIDATION_FAILED","Batch panel edit changed the single-material editor or material slots")
    resources = {}
    with ExitStack() as guards:
        for assignment in batch["assignments"]:
            for image in image_documents(assignment):
                path = guard_file(image["file"],guards,image["expected_sha256"])
                resources[str(path)] = image["expected_sha256"]
    external = panel_dependencies(bpy)
    panel = batch_panel_state(scene,ui_batch)
    retained = []
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
            item.use_fake_user = True
            retained.append(identity(item))
    stage = Path(request["candidate_stage"])
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=str(stage),copy=True,relative_remap=True,check_existing=False)=={"FINISHED"},
            "IO_ERROR","Could not save the panel candidate")
    cold = open_context(bpy,{**request,"file":str(stage)})
    for label, actual, expected in (
        ("preview controls", batch_panel_state(cold.scene, ui_batch), panel),
        ("batch", ui_batch.get_manifest(cold.scene), batch),
        ("single editor", saved_editor(cold.scene, ui), expected_editor),
        ("material slots", slot_state(bpy), before_slots),
    ):
        require(actual == expected, "VALIDATION_FAILED", f"Reopened {label} differs")
    reopened_paths = panel_dependencies(bpy)
    require(reopened_paths == external, "VALIDATION_FAILED",
            f"Reopened dependency paths differ: removed={sorted(external-reopened_paths)}, added={sorted(reopened_paths-external)}")
    return {"manifest":batch,"panel":panel,"saved_single_editor":expected_editor,"retained_orphans":retained,
            "previous_editor_assignment":before_editor["assignment_id"],"edit":op,
            "context_matches":batch["context"]==request["context"],"candidate_sha256":digest_file(stage),
            "resources":[{"file":p,"expected_sha256":s} for p,s in sorted(resources.items())]}


def batch_panel_state(scene, ui_batch):
    batch = ui_batch.get_manifest(scene)
    index = scene.mw_batch_view_index
    state = {"assignment_id":batch["assignments"][scene.mw_batch_index]["id"],"view_id":batch["preview"]["views"][index]["id"]}
    state.update(object=scene.mw_batch_object, material_slot=scene.mw_batch_slot, uv_layer=scene.mw_batch_uv,
                 faces_text=scene.mw_batch_faces, dependencies_text=scene.mw_batch_dependencies)
    for key in ("width","height","samples","engine","azimuth","elevation","lighting","focus","zoom"):
        value = getattr(scene,"mw_batch_"+key)
        state[key] = list(value) if key=="focus" else value
    return state


def work(request, scratch):
    import bpy

    require(bpy.app.background and not bpy.app.online_access and
            not bpy.context.preferences.filepaths.use_scripts_auto_execute,
            "UNSUPPORTED", "Expected offline background Blender with automatic scripts disabled")
    product = select_product(request, bpy)
    from material_workflow_addon import ui, ui_batch
    from material_workflow_addon.contract import image_documents

    ui.register()
    ui_batch.register()
    context = open_context(bpy, request)
    scene = context.scene
    if request["action"] == "edit-batch-panel":
        return {**edit_panel(bpy,context,ui,ui_batch,request),"product":product,
                "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    baseline = ui_batch.get_manifest(scene)
    ui_batch.validate_context(context, baseline)
    prior_editor = saved_editor(scene, ui)
    if request["action"] == "export-batch-draft":
        manifest, details = baseline, {"consumer": "material batch"}
    else:
        manifest, details = assignment_output(bpy, context, baseline, request, product)
    if request["action"] != "edit-batch-assignment":
        manifest = export_batch(bpy, scene, manifest, scratch)
    if request["action"] == "export-batch-draft":
        require(saved_editor(scene, ui) == prior_editor and ui_batch.get_manifest(scene) == baseline,
                "VALIDATION_FAILED", "Export implicitly changed the saved draft")
    documents = manifest.get("assignments", [manifest])
    resources = {}
    with ExitStack() as guards:
        for part in documents:
            for image in image_documents(part):
                if image["file"] not in resources:
                    path = guard_file(image["file"], guards, image["expected_sha256"])
                    resources[str(path)] = image["expected_sha256"]
    return {
        "manifest": manifest, **details, "product": product, "saved_single_editor": prior_editor,
        "batch_origin_sha256": fingerprint(baseline),
        "resources": [{"file": path, "expected_sha256": sha} for path, sha in sorted(resources.items())],
        "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()},
    }


def worker(request_path, result_path):
    result = Path(result_path)
    try:
        response = {"data": work(read_json(request_path), result.parent)}
    except Exception as exc:
        response = {"error": failure(exc)}
    atomic_json(result, response)


def execute_worker(blender, job, timeout):
    profile = job / "profile"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("BLENDER_USER_", "BLENDER_SYSTEM_", "PYTHON"))}
    environment.update(BLENDER_USER_RESOURCES=str(profile), PYTHONDONTWRITEBYTECODE="1",
                       TMP=str(job), TEMP=str(job), TMPDIR=str(job))
    result = job / "worker-result.json"
    argv = [str(blender), "--background", "--factory-startup", "--offline-mode", "--disable-autoexec",
            "--threads", "2", "--python-exit-code", "23", "--python", str(ENTRY), "--", WORKER_MARKER,
            str(job / "request.json"), str(result)]
    with (job / "worker.log").open("xb") as log:
        process = ProcessTree(argv, cwd=str(job), env=environment, stdin=subprocess.DEVNULL,
                              stdout=log, stderr=subprocess.STDOUT)
        try:
            code = process.process.wait(timeout=timeout)
        except BaseException:
            process.terminate()
            raise
        finally:
            process.close()
    require(code == 0 and result.is_file(), "WORKER_FAILED", f"Blender worker exited {code}. See worker.log")
    envelope = read_json(result)
    if "error" in envelope:
        from protocol import Failure

        raise Failure(envelope["error"]["code"], envelope["error"]["message"])
    return envelope["data"]


def run(args):
    job = args.jobs_dir.absolute() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    command = "material." + args.action
    data = error = None
    artifacts = {"job_directory": str(job), "result": str(job / "result.json")}
    try:
        timeout = timeout_value(args.timeout)
        suffix = ".blend" if args.action=="edit-batch-panel" else ".json"
        require(args.output.is_absolute() and args.output.suffix.lower()==suffix, "INVALID_REQUEST",
                f"Output requires a new absolute {suffix} path")
        with ExitStack() as guards:
            request = inputs(args, guards)
            parent = canonical(args.output.parent)
            output = parent / args.output.name
            guards.enter_context(FileGuard(parent, directory=True))
            guards.enter_context(PathLocks([output]))
            require(not os.path.lexists(output), "CONFLICT", f"Output already exists: {output}")
            if suffix == ".blend":
                with tempfile.NamedTemporaryFile(dir=parent,prefix=".batch-draft-",suffix=suffix,delete=False) as tmp:
                    staged = Path(tmp.name)
                guards.callback(staged.unlink,missing_ok=True)
                request["candidate_stage"] = str(staged)
            else:
                directory = guards.enter_context(tempfile.TemporaryDirectory(prefix=".batch-draft-", dir=parent))
                staged = Path(directory)/"candidate.json"
            atomic_json(job / "request.json", request)
            result = execute_worker(canonical(args.blender), job, timeout)
            if suffix == ".blend":
                manifest = job/"batch.json"
                atomic_json(manifest,result.pop("manifest"))
                result["batch_manifest"] = {"file":str(manifest),"sha256":digest_file(manifest)}
            else:
                atomic_json(staged,result.pop("manifest"))
            with FileGuard(staged, renameable=True) as guard:
                sha = guard.sha256()
                if suffix == ".blend":
                    require(sha==result.pop("candidate_sha256"),"CONFLICT","Candidate changed after reopen")
                guard.rename_new(output)
            artifacts.update(output=str(output), worker_result=str(job / "worker-result.json"))
            data = {**result, "output": {"file": str(output), "sha256": sha},
                    "source": {k: request[k] for k in ("file", "expected_sha256")},
                    "context": request["context"], "source_unchanged": True}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Material batch draft worker timed out"}
    except (ValueError, OSError, RuntimeError) as exc:
        error = failure(exc, "IO_ERROR")
    except Exception as exc:
        error = failure(exc)
    if (job / "worker.log").exists():
        artifacts["worker_log"] = str(job / "worker.log")
    document = envelope(command, job.name, data, error, artifacts)
    atomic_json(job / "result.json", document)
    return document


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except Exception as exc:
        result = envelope(error=failure(exc, "IO_ERROR"))
    if args and args.human:
        print(result["artifacts"]["output"] if result["ok"] else result["error"]["message"])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES[result["error"]["code"]]


if __name__ == "__main__":
    if WORKER_MARKER in sys.argv and "--background" in sys.argv:
        worker(*sys.argv[sys.argv.index(WORKER_MARKER) + 1:])
    else:
        sys.exit(main())
