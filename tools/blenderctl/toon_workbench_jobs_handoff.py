"""Prepare native Toon jobs and receive accepted style into protected saved copies."""
import json
import os
from pathlib import Path
import re

from filesystem import FileGuard
from material_draft_handoff import canonical, fingerprint, guard_file, open_saved_context, read_document, require
from protocol import Failure, digest, read_json
import toon_viewport_lines_contract as contract

READ = "inspect-workbench-author"
PREPARE = "prepare-workbench-job"
RECEIVE = "receive-workbench-author"
REFRESH = "refresh-workbench-job"
OPERATIONS = (READ, PREPARE, RECEIVE, REFRESH)
JOB_ID = re.compile(r"[0-9a-f]{32}")
TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "timed_out", "abandoned"}
HELP = ("prepare-workbench-job invokes the native snapshot panel with job_settings and job_mode. "
        "inspect-workbench-author reports the saved submission owner and file when present. "
        "refresh-workbench-job joins a SHA-pinned submission to its scene, status and terminal result, optionally with job_id. "
        "Set use_saved_owner=true to follow a matching native workbench submission from a renamed or saved-as project. "
        "receive-workbench-author requires SHA-pinned submission and edit receipt, matching author SHA and a new .blend.")


def file_ref(ref, expected_suffix=".json"):
    require(type(ref) is dict and set(ref) == {"file", "sha256"}
            and isinstance(ref["file"], str) and Path(ref["file"]).is_absolute() and Path(ref["file"]).suffix.lower() == expected_suffix
            and isinstance(ref["sha256"], str) and contract.SHA256.fullmatch(ref["sha256"]),
            "INVALID_REQUEST", "Expected an absolute " + expected_suffix + " file and its SHA-256")
    return dict(ref)


def normalize_request(document, editions):
    dedicated = {"job_mode", "job_settings", "submission", "edit_receipt", "resources", "job_id", "use_saved_owner"}
    operation = document["operation"]
    core = {key: value for key, value in document.items() if key not in dedicated}
    require(not {"patch", "scope", "frames", "elements"} & core.keys(), "INVALID_REQUEST", "Use native workbench settings")
    if operation == RECEIVE:
        require(dedicated & document.keys() == {"submission", "edit_receipt"}, "INVALID_REQUEST",
                "Receive needs the prepared submission and accepted edit receipt")
        core.update(operation="render-camera")
    else:
        core.update(operation="inspect", scope={"groups": ["workbench-job"]})
    result = contract.normalize_request(core, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == READ:
        require(not dedicated & document.keys(), "INVALID_REQUEST", "Inspection accepts no workbench edits")
    if operation == RECEIVE:
        result.update(submission=file_ref(document["submission"]), edit_receipt=file_ref(document["edit_receipt"]))
    if operation == REFRESH:
        require({"submission"} <= dedicated & document.keys() <= {"submission", "job_id", "use_saved_owner"}, "INVALID_REQUEST",
                "Refresh needs a pinned submission and optionally its public job ID")
        use_saved_owner = document.get("use_saved_owner", False)
        require(type(use_saved_owner) is bool, "INVALID_REQUEST", "use_saved_owner must be a boolean")
        job_id = document.get("job_id")
        require(job_id is None or isinstance(job_id, str) and JOB_ID.fullmatch(job_id), "INVALID_REQUEST",
                "Expected a 32-digit lowercase public job ID")
        result.update(submission=file_ref(document["submission"]), job_id=job_id, use_saved_owner=use_saved_owner)
    if operation == PREPARE:
        require({"job_mode", "job_settings"} <= dedicated & document.keys() <= {"job_mode", "job_settings", "resources"} and
                document["job_mode"] in ("run", "edit", "inspect"), "INVALID_REQUEST", "Unknown workbench job mode")
        values = document["job_settings"]
        fields = {"project_directory", "output_directory", "render_mode", "sequence_times", "style_file",
                  "timeout_seconds", "samples_per_axis", "lighting_samples", "memory_budget_mb"}
        require(type(values) is dict and {"project_directory", "output_directory"} <= values.keys() <= fields,
                "INVALID_REQUEST", "Expected workbench directories and explicit native settings")
        vals = dict(values)
        mode = vals.setdefault("render_mode", "FRAME")
        require(mode in ("FRAME", "SEQUENCE") and ("sequence_times" in vals) == (mode == "SEQUENCE"),
                "INVALID_REQUEST", "SEQUENCE requires sequence_times, FRAME uses the saved frame")
        if "sequence_times" in vals:
            require(type(vals["sequence_times"]) is list and 1 <= len(vals["sequence_times"]) <= 96,
                    "INVALID_REQUEST", "Expected up to 96 native sequence times")
        for key, low, high in (("timeout_seconds", 10, 86400), ("lighting_samples", 1, 256), ("memory_budget_mb", 32, 65536)):
            if key in vals:
                require(type(vals[key]) is int and low <= vals[key] <= high, "INVALID_REQUEST", "Out of range " + key)
        if "samples_per_axis" in vals:
            require(type(vals["samples_per_axis"]) is int and vals["samples_per_axis"] in (1, 2, 4, 8), "INVALID_REQUEST",
                    "Unsupported workbench samples_per_axis")
        require(("style_file" in vals) == (document["job_mode"] == "edit"), "INVALID_REQUEST", "Only edit uses style_file")
        if "style_file" in vals:
            vals["style_file"] = file_ref(vals["style_file"])
        resources = document.get("resources", [])
        require(type(resources) is list and len(resources) <= 256, "INVALID_REQUEST", "Expected bounded resource pins")
        result.update(job_mode=document["job_mode"], job_settings=vals, resources=resources)
    return result


def pin_json(ref, guards):
    return read_document(guard_file(ref["file"], guards, ref["sha256"]))


def read_workbench_state(request, guards):
    ref = request["submission"]
    record = pin_json(ref, guards)
    require(type(record) is dict and record.get("schema") == "toon-workbench-submission.v1"
            and isinstance(record.get("owner"), str) and JOB_ID.fullmatch(record["owner"]),
            "INVALID_REQUEST", "Expected a native owned workbench submission")
    path = Path(ref["file"])
    directory = path.parent
    jobs = directory / "jobs"
    snapshot = record.get("snapshot")
    command = "toon-workflow." + str(record.get("operation"))
    require(path.name == "submission.json" and directory.name == "toon-joint-" + record["owner"]
            and Path(record["directory"]) == directory and Path(record["jobs_directory"]) == jobs
            and Path(record["request"]) == directory / "request.json"
            and command in {"toon-workflow.run", "toon-workflow.edit", "toon-workflow.inspect", "toon-workflow.recover"},
            "CONFLICT", "Submission paths or owner do not match the native workbench")
    context = request["context"]
    # For a saved-as project the worker compares the reopened native panel to this pinned submission.
    known_project = any(target and Path(target) == Path(request["file"]) for target in (record.get("source_filepath"), snapshot))
    require(all(context[key] == record[key] for key in ("scene", "view_layer"))
            and (known_project or request.get("use_saved_owner", False)),
            "CONFLICT", "The workbench submission belongs to a different saved project, scene or view layer")
    if snapshot and Path(request["file"]) == Path(snapshot):
        require(request["expected_sha256"] == record.get("snapshot_sha256"), "CONFLICT", "The native snapshot SHA differs")
    original = read_document(guard_file(directory / "request.json", guards))
    require(type(original) is dict and original.get("command") == command and type(original.get("params")) is dict,
            "CONFLICT", "Stored request and native operation differ")
    params = original["params"]
    if command == "toon-workflow.recover":
        require(params.get("recovery") and params["recovery"] == record.get("recovery_ref"),
                "CONFLICT", "Recovery request does not match the owned submission")
    else:
        manifest = params.get("manifest", {})
        require(snapshot and params.get("file") == snapshot and params.get("expected_sha256") == record.get("snapshot_sha256")
                and all(record[key] == manifest.get(key) for key in ("scene", "view_layer", "times")),
                "CONFLICT", "Stored request names different snapshot, scene or render times")
    recorded_id = record.get("job_id")
    owner_state = record.get("state", "prepared")
    submission_log = directory / "submit-stdout.log"
    submission_error = None
    if not recorded_id and submission_log.exists():
        owner_state = "submitting"
        log = canonical(submission_log)
        try:
            admission = read_json(log)
        except Failure as error:
            if error.code != "INVALID_REQUEST" or "Invalid UTF-8 JSON:" not in str(error):
                raise
        else:
            require(type(admission) is dict, "CONFLICT", "Native submit log must contain a public receipt")
            if admission.get("ok") is False:
                owner_state = "submit_failed"
                submission_error = admission.get("error")
            else:
                recorded_id = admission.get("job_id")
                require(isinstance(recorded_id, str) and JOB_ID.fullmatch(recorded_id),
                        "CONFLICT", "Native submit log contains no valid public job ID")
    requested_id = request["job_id"]
    require(not recorded_id or requested_id in (None, recorded_id), "CONFLICT", "Requested job differs from the recorded job")
    job_id = recorded_id or requested_id
    state = {"owner": record["owner"], "submission": ref, "operation": record["operation"], "state": owner_state,
             "job_id": job_id, "scene": record["scene"], "view_layer": record["view_layer"], "times": record["times"],
             "snapshot": {"file": snapshot, "sha256": record["snapshot_sha256"]}, "artifact": None,
             "request_sha256": fingerprint(original), "status_file": None, "result_file": None, "error": submission_error}
    bundle_sha = params.get("implementation", {}).get("bundle.json")
    require(isinstance(bundle_sha, str) and contract.SHA256.fullmatch(bundle_sha),
            "CONFLICT", "The submitted job has no frozen Toon bundle identity")
    if job_id is None:
        return state, bundle_sha
    require(isinstance(job_id, str) and JOB_ID.fullmatch(job_id), "CONFLICT", "Invalid job ID in the owned submission")
    job = jobs / job_id
    job_request = job / "request.json"
    require(job_request.is_file(), "NOT_FOUND", "Public job is absent from the owned submission")
    effective = read_document(guard_file(job_request, guards))
    require(fingerprint(effective) == state["request_sha256"], "CONFLICT", "Public job request differs from the owned submission")
    status_path = job / "status.json"
    if not status_path.exists():
        return {**state, "state": "submitted"}, bundle_sha
    status = read_document(canonical(status_path))
    require(type(status) is dict and status.get("job_id") == job_id and isinstance(status.get("state"), str),
            "CONFLICT", "Public status belongs to a different job")
    state.update(state=status["state"], status_file=str(status_path))
    if status["state"] not in TERMINAL:
        return state, bundle_sha
    result_path = job / "result.json"
    if not result_path.exists():
        return state, bundle_sha
    result = read_json(guard_file(result_path, guards))
    require(type(result) is dict and result.get("job_id") == job_id and result.get("command") == command
            and type(result.get("ok")) is bool and result["ok"] == (status["state"] == "succeeded"),
            "CONFLICT", "Public terminal result belongs to a different job or state")
    state.update(result_file=str(result_path), error=result.get("error"))
    if result["ok"]:
        data = result.get("data") or {}
        output = data.get("output") or (data.get("result") or {}).get("output") or result.get("artifacts", {}).get("output-toon-joint.png")
        if output:
            artifact = canonical(output)
            require(artifact.is_relative_to(job) and artifact.is_file(), "CONFLICT", "Public result points outside its owned job")
            state["artifact"] = str(artifact)
    return state, bundle_sha


def guard_receive(request, guards):
    submission = pin_json(request["submission"], guards)
    public = pin_json(request["edit_receipt"], guards)
    require(type(submission) is dict and submission.get("schema") == "toon-workbench-submission.v1"
            and submission.get("operation") == "edit" and type(public) is dict and public.get("ok") is True
            and public.get("command") == "toon-workflow.edit", "INVALID_REQUEST", "Expected an accepted native edit and its prepared snapshot")
    data = public.get("data")
    require(type(data) is dict and data.get("candidate_accepted") is True
            and data.get("parent_output_validation") == "passed" and data.get("baseline_author_sha256") == submission.get("author_sha256"),
            "CONFLICT", "Accepted job has a different prepared author baseline")
    job = Path(public["artifacts"]["job_directory"])
    root = Path(submission["jobs_directory"])
    require(job.parent == root and job.name == public["job_id"] and Path(request["edit_receipt"]["file"]) == job / "result.json",
            "CONFLICT", "Receipt is outside the prepared job or no longer names the registered result")
    acceptance = read_document(guard_file(job / "toon-workflow-acceptance.json", guards))
    report_ref = acceptance.get("report", {})
    require(acceptance.get("status") == "accepted" and acceptance.get("operation") == "edit"
            and Path(data["acceptance"]) == job / "toon-workflow-acceptance.json"
            and acceptance.get("outputs") == data.get("outputs")
            and report_ref.get("file") == str(job / "toon-workflow-report.json"), "CONFLICT", "Edit acceptance identity differs")
    report = read_document(guard_file(report_ref["file"], guards, report_ref["expected_sha256"]))
    effective_request = read_document(guard_file(job / "request.json", guards))
    original_request = read_document(guard_file(submission["request"], guards))
    require(fingerprint(effective_request) == acceptance["request_sha256"] == fingerprint(original_request)
            and effective_request["command"] == "toon-workflow.edit", "CONFLICT", "Prepared request changed from the accepted job")
    baseline = {"file": submission["snapshot"], "expected_sha256": submission["snapshot_sha256"]}
    require(report["source"] == baseline and report["operation"] == "edit"
            and report["implementation"] == effective_request["params"]["implementation"]
            and report["result"]["style"] == data["style"]
            and report["result"]["author_sha256"] == data["author_sha256"]
            and report["result"]["baseline_author_sha256"] == request["expected_author_sha256"] == submission["author_sha256"]
            and request["context"]["scene"] == submission["scene"] and request["context"]["view_layer"] == submission["view_layer"],
            "CONFLICT", "Target author, saved scene or accepted source differs")
    style = data["style"]
    require(Path(style["file"]) == job / "toon-author-style.json" and style in acceptance["outputs"],
            "CONFLICT", "Accepted style is missing or escaped its job")
    request["accepted_author"] = {"style": str(guard_file(style["file"], guards, style["expected_sha256"])),
        "baseline": str(guard_file(baseline["file"], guards, baseline["expected_sha256"])),
        "receipt": request["edit_receipt"], "after": data["author_sha256"], "before": submission["author_sha256"],
        "bundle_sha256": report["implementation"].get("bundle.json")}


def guard_inputs(request, guards):
    if request["operation"] == RECEIVE:
        guard_receive(request, guards)
    elif request["operation"] == REFRESH:
        request["refreshed_job"], request["workbench_bundle_sha256"] = read_workbench_state(request, guards)
    elif request["operation"] == PREPARE:
        panel = request["job_settings"]
        for key in ("project_directory", "output_directory"):
            path = canonical(panel[key])
            require(path.is_dir(), "INVALID_REQUEST", "Expected existing workbench " + key)
            guards.enter_context(FileGuard(path, directory=True))
            panel[key] = str(path)
        require((Path(panel["project_directory"]) / "blenderctl.ps1").is_file(), "INVALID_REQUEST", "Workbench project lacks blenderctl.ps1")
        if "style_file" in panel:
            ref = panel["style_file"]
            panel["style_file"] = str(guard_file(ref["file"], guards, ref["sha256"]))
        for row in request["resources"]:
            require(type(row) is dict and set(row) == {"file", "expected_sha256"}, "INVALID_REQUEST", "Resource needs file and SHA")
            row["file"] = str(guard_file(row["file"], guards, row["expected_sha256"]))


def animation_state(scene):
    from inspection import record
    from toon_workflow.toon_viewport_preview import group_ui

    ad = scene.animation_data
    actions = {ad.action} if ad and ad.action else set()
    if ad:
        actions.update(strip.action for track in ad.nla_tracks for strip in track.strips if strip.action)
    return {"bindings": record(scene).get("animation"), "actions": [record(action) for action in sorted(actions, key=lambda a: a.name)],
            "paths": sorted(group_ui._animation_paths(scene))}


def state(context, author, workflow):
    return {"author_sha256": author, "non_author_sha256": workflow.non_author_signature(context.scene),
            "native_author_animation_sha256": fingerprint(animation_state(context.scene))}


def prepare(context, request, workbench):
    panel = getattr(context.scene, workbench.PROP)
    allowed = {os.path.normcase(row["file"]) for row in request["resources"]}
    require(all(os.path.normcase(path) in allowed for path in workbench.collect_resources()), "CONFLICT", "Unpinned workbench inputs")
    for key, value in request["job_settings"].items():
        if key == "sequence_times":
            value = json.dumps(value, allow_nan=False)
        elif key == "samples_per_axis":
            value = str(value)
        setattr(panel, key, value)
    result = workbench._prepare(context, request["job_mode"], activate=False)
    require(result["snapshot_sha256"] == digest(result["snapshot"]) and result["source_unchanged"]
            and result["source_sha256"] == request["expected_sha256"], "VALIDATION_FAILED", "Native snapshot did not preserve the saved source")
    directory = Path(result["directory"])
    product = request["product"]
    bundle_variable = "BLENDERCTL_TOON_BUNDLE_521" if request["edition"] == "0.51.1" else "BLENDERCTL_TOON_BUNDLE"
    launcher = {"program": str(Path(result["project_root"]) / "blenderctl.ps1"),
                "environment": {bundle_variable: product["directory"]},
                "arguments_before_params": ["--blender-version", ".".join(map(str, product["host"])),
                    "--jobs-dir", result["jobs_directory"], "--timeout", str(panel.timeout_seconds),
                    "--transactions-dir", str(directory / "transactions"),
                    "toon-workflow", request["job_mode"], "--params"]}
    return {"preparation": result, "submission": {"file": str(directory / "submission.json"), "sha256": digest(directory / "submission.json")},
            "workflow_request": {"file": result["request"], "sha256": digest(result["request"])},
            "launcher_for_request_params": launcher, "saved_options": request["job_settings"]}


def saved_submission(scene, workbench):
    panel = getattr(scene, workbench.PROP)
    owner = panel.owner_token
    directory = Path(panel.submission_directory)
    if JOB_ID.fullmatch(owner) and directory.is_absolute() and directory.name == "toon-joint-" + owner:
        return {"owner": owner, "file": str(directory / "submission.json")}
    return None


def work(bpy, context, request, author, *, author_sha, save_candidate, read_bindings, dependencies):
    from toon_workflow.toon_workflow_addon import author_receive, joint_workflow, workbench

    op = request["operation"]
    response = {"operation": op, "product": request["product"], "context": request["context"],
                "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
                "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if op == REFRESH:
        require(request["workbench_bundle_sha256"] == request["product"]["manifest_sha256"],
                "CONFLICT", "Prepared public job uses a different Toon edition")
        if request.get("use_saved_owner", False):
            pointer = saved_submission(context.scene, workbench)
            require(pointer is not None and pointer["owner"] == request["refreshed_job"]["owner"]
                    and Path(pointer["file"]) == Path(request["submission"]["file"]),
                    "CONFLICT", "Reopened scene does not own the pinned native workbench submission")
            try:
                workbench._record(getattr(context.scene, workbench.PROP))
            except ValueError as error:
                raise Failure("CONFLICT", str(error)) from error
        return {**response, "workbench": request["refreshed_job"], "author_sha256": author}
    before = state(context, author, joint_workflow)
    rows, refs = read_bindings(), dependencies()
    scene_sha = fingerprint([rows, refs])
    if op == READ:
        observed = fingerprint(before)
        require(observed == request.get("verify_state_sha256", observed) and scene_sha == request.get("verify_scene_sha256", scene_sha),
                "VALIDATION_FAILED", "Independent reopen changed the received author or geometry")
        return {**response, "state": before, "author_sha256": author, "state_sha256": observed, "scene_sha256": scene_sha,
                "saved_submission": saved_submission(context.scene, workbench)}
    if op == PREPARE:
        try:
            result = prepare(context, request, workbench)
        except (TypeError, ValueError, RuntimeError) as error:
            raise Failure("CONFLICT", str(error)) from error
        return {**response, **result}
    accepted = request["accepted_author"]
    require(accepted["bundle_sha256"] == request["product"]["manifest_sha256"] and author == accepted["before"],
            "CONFLICT", "The accepted result and current author do not share the selected edition and baseline")
    baseline = open_saved_context(bpy, {**request, "file": accepted["baseline"]})
    baseline_state = state(baseline, author_sha(baseline.scene), joint_workflow)
    require(baseline_state["author_sha256"] == before["author_sha256"]
            and baseline_state["native_author_animation_sha256"] == before["native_author_animation_sha256"]
            and read_bindings() == rows and dependencies() == refs, "CONFLICT",
            "Saved author, animation, named objects or material bindings changed since preparation")
    context = open_saved_context(bpy, request)
    try:
        author_receive.apply(context.scene, accepted["style"], accepted["after"], before["non_author_sha256"])
    except (KeyError, ValueError, TypeError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error
    after = state(context, author_sha(context.scene), joint_workflow)
    require(after["author_sha256"] == accepted["after"]
            and {k: v for k, v in before.items() if k != "author_sha256"} == {k: v for k, v in after.items() if k != "author_sha256"}
            and read_bindings() == rows and dependencies() == refs, "VALIDATION_FAILED", "Author receive changed protected geometry or animation")
    retained = save_candidate(bpy, request)
    return {**response, "state": after, "event": {"status": "received", "receipt": accepted["receipt"]},
            "author_before_sha256": author, "author_after_sha256": after["author_sha256"], "scene_sha256": scene_sha,
            "state_sha256": fingerprint(after), "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
            "protected": {"status": "passed", "current_geometry_and_animation": True}}
