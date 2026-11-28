"""Blender worker for animation.project-events review and cold verification.

The worker selects the bundled native author by the launcher's edition.
The caller's request cannot select an arbitrary runtime or Python entry.
"""
import hashlib
import json
import os
import sys
import traceback
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CORE
from host_compat import is_supported_host
from project_probe import (EPSILON, compact_probe, context_call, digest, group_map,
                           probe, read_json, resource_manifest,
                           stable_review_digest, write_json)


PLAN_SCHEMA = "animation-project-events-plan.v1"
RUN_ACTIONS = ("capture", "adopt", "retime", "set_relations", "set_lock")


class WorkerError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def fail(error):
    code = getattr(error, "code", None) or "WORKER_FAILED"
    details = getattr(error, "details", None)
    return {
        "code": code if isinstance(code, str) else "WORKER_FAILED",
        "message": str(error),
        "details": details if isinstance(details, dict) else {},
    }


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def timing(scene):
    return {
        "fps": int(scene.render.fps),
        "fps_base": float(scene.render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
    }


def event_map(value):
    return {row["event_id"]: row for row in value["events"]}


def same_slot(left, right):
    return all(left.get(key) == right.get(key) for key in (
        "target", "slot_identifier", "slot_type", "data_path", "array_index"))


def check(rows, name, condition, details=None):
    row = {"name": name, "passed": bool(condition)}
    if details is not None:
        row["details"] = details
    rows.append(row)
    return bool(condition)


def key_window_for(manifest):
    if manifest["action"] != "retime":
        return None
    event = manifest["clip"]["event"]
    return [float(event["start"]), float(event["end"])]


def verify_change(plan, before, after):
    checks = []
    action = plan["action"]
    request = plan["manifest"]
    before_events = event_map(before)
    after_events = event_map(after)
    selected = request.get("event_id")

    check(checks, "store_id", after["store"]["id"] == before["store"]["id"])
    check(checks, "store_revision",
          after["store"]["revision"] == before["store"]["revision"] + 1,
          {"before": before["store"]["revision"],
           "after": after["store"]["revision"]})
    before_timing = {key: before["timing"].get(key) for key in plan["timing"]}
    after_timing = {key: after["timing"].get(key) for key in plan["timing"]}
    check(checks, "timing_unchanged",
          before_timing == after_timing == plan["timing"],
          {"before": before_timing, "after": after_timing,
           "plan": plan["timing"]})
    check(checks, "scene_context_unchanged",
          (after["scene"], after["view_layer"])
          == (before["scene"], before["view_layer"]))

    if action == "capture":
        check(checks, "capture_adds_one_event",
              len(after_events) == len(before_events) + 1 and selected in after_events)
        check(checks, "capture_preserves_existing_events",
              all(after_events.get(event_id) == row
                  for event_id, row in before_events.items()))
        check(checks, "capture_contact",
              abs(float(after_events[selected]["contact"])
                  - float(request["clip"]["event"]["contact"])) <= EPSILON)
    else:
        check(checks, "event_inventory_unchanged",
              set(after_events) == set(before_events))
        for event_id in sorted(set(before_events) & set(after_events)):
            if event_id != selected:
                left, right = before_events[event_id], after_events[event_id]
                check(checks, "other_event_times_" + event_id,
                      all(left.get(key) == right.get(key) for key in (
                          "start", "contact", "end", "locked")))
                check(checks, "other_event_geometry_" + event_id,
                      left["geometry_metadata_sha256"]
                      == right["geometry_metadata_sha256"])
                if action != "retime":
                    check(checks, "other_event_record_" + event_id,
                          left["record_digest"] == right["record_digest"])

    if action == "retime":
        left, right = before_events[selected], after_events[selected]
        target = float(request["clip"]["event"]["target_contact"])
        check(checks, "retime_contact",
              abs(float(right["contact"]) - target) <= EPSILON,
              {"target": target, "actual": right["contact"]})
        check(checks, "retime_window_unchanged",
              all(left.get(key) == right.get(key)
                  for key in ("start", "end", "locked")))
        check(checks, "retime_record_changed",
              left["record_digest"] != right["record_digest"])
        check(checks, "retime_selected_geometry",
              left["geometry_metadata_sha256"]
              == right["geometry_metadata_sha256"]
              if "contact_intent" not in request and "spatial_locks" not in request
              else True)
    elif action == "set_lock":
        left, right = before_events[selected], after_events[selected]
        check(checks, "set_lock_value",
              bool(right["locked"]) == bool(request["locked"]),
              {"expected": bool(request["locked"]), "actual": bool(right["locked"])})
        check(checks, "set_lock_contacts_unchanged",
              all(left.get(key) == right.get(key)
                  for key in ("start", "contact", "end")))
        check(checks, "set_lock_record_unchanged",
              left["record_digest"] == right["record_digest"])
    elif action == "set_relations":
        check(checks, "set_relations_value",
              after["relations"] == request["relations"])
        check(checks, "set_relations_contacts_unchanged",
              all(before_events[event_id]["contact"] == row["contact"]
                  for event_id, row in after_events.items()))
        check(checks, "set_relations_records_unchanged",
              all(before_events[event_id]["record_digest"] == row["record_digest"]
                  for event_id, row in after_events.items()))
    elif action == "adopt":
        left, right = before_events[selected], after_events[selected]
        if "lock_contact" not in request:
            check(checks, "adopt_lock_unchanged", left["locked"] == right["locked"])
        if "contact_intent" not in request and "spatial_locks" not in request:
            check(checks, "adopt_geometry_unchanged",
                  left["geometry_metadata_sha256"]
                  == right["geometry_metadata_sha256"])
        check(checks, "adopt_record_changed",
              left["record_digest"] != right["record_digest"])

    expected_relations = (request["relations"] if action == "set_relations"
                          else before["relations"])
    check(checks, "relations_policy", after["relations"] == expected_relations)

    before_groups = group_map(before)
    after_groups = group_map(after)
    inventory_ok = (set(before_groups) <= set(after_groups)
                    if action == "capture"
                    else set(before_groups) == set(after_groups))
    check(checks, "key_target_inventory", inventory_ok,
          {"before": len(before_groups), "after": len(after_groups)})
    for target in sorted(before_groups):
        if target not in after_groups:
            continue
        left, right = before_groups[target], after_groups[target]
        check(checks, "slot_identity_" + target, same_slot(left, right))
        check(checks, "slot_key_count_" + target,
              left["key_count"] == right["key_count"])
        if action == "retime":
            check(checks, "outside_keys_" + target,
                  left["outside_stable_sha256"]
                  == right["outside_stable_sha256"])
        else:
            check(checks, "all_keys_" + target,
                  left["all_stable_sha256"] == right["all_stable_sha256"])

    if action == "retime":
        inside_changed = any(
            before_groups[target]["inside_stable_sha256"]
            != after_groups[target]["inside_stable_sha256"]
            for target in set(before_groups) & set(after_groups))
        check(checks, "retime_changed_window_keys", inside_changed)
    elif action == "capture":
        check(checks, "capture_native_key_total",
              before["keyframe_total"] == after["keyframe_total"],
              {"before": before["keyframe_total"],
               "after": after["keyframe_total"]})
    else:
        check(checks, "no_native_key_change",
              before["key_protection"]["sha256"]
              == after["key_protection"]["sha256"])

    if not all(row["passed"] for row in checks):
        raise WorkerError(
            "VALIDATION_FAILED",
            "candidate differs from the reviewed plan: "
            + ", ".join(row["name"] for row in checks if not row["passed"]),
            {"checks": checks})
    return checks


# action: PF-02 stage resource SHA and resolved-path comparison.
def compare_resources(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise WorkerError("CONFLICT", "resource manifest is missing")
    expected_rows = expected.get("rows")
    actual_rows = actual.get("rows")
    if not isinstance(expected_rows, list) or not isinstance(actual_rows, list):
        raise WorkerError("CONFLICT", "resource manifest rows are malformed")
    expected_map = {}
    for row in expected_rows:
        if not isinstance(row, dict):
            raise WorkerError("CONFLICT", "resource row is malformed")
        if not row.get("exists"):
            raise WorkerError(
                "NOT_FOUND",
                "planned external resource is unresolved: "
                + str(row.get("raw")), {"resource": row})
        expected_map[row["resolved"]] = row.get("sha256")
    actual_map = {
        row["resolved"]: row.get("sha256")
        for row in actual_rows if isinstance(row, dict) and row.get("exists")
    }
    if set(expected_map) != set(actual_map):
        raise WorkerError(
            "CONFLICT",
            "resolved external resource set differs from the plan",
            {"missing": sorted(set(expected_map) - set(actual_map)),
             "extra": sorted(set(actual_map) - set(expected_map))})
    bad = [path for path, value in expected_map.items()
           if actual_map.get(path) != value]
    if bad:
        raise WorkerError(
            "CONFLICT", "resolved external resource SHA differs from the plan",
            {"resources": bad})
    return {"status": "passed", "resources": len(expected_map)}


# action: PF-02 Blender remap save into the final parent.
def run_stage(request):
    import bpy
    candidate = Path(request["file"]).resolve()
    if file_sha256(candidate) != request["expected_sha256"]:
        raise WorkerError("CONFLICT", "candidate SHA changed before staging")
    stage = Path(request["stage"]).resolve()
    if os.path.lexists(stage):
        raise WorkerError("CONFLICT", "stage already exists: " + str(stage))
    bpy.ops.wm.open_mainfile(filepath=str(candidate))
    saved = bpy.ops.wm.save_as_mainfile(
        filepath=str(stage), copy=True, relative_remap=True,
        check_existing=False)
    if saved != {"FINISHED"} or not stage.is_file():
        raise WorkerError("IO_ERROR", "could not create the remapped stage")
    return {
        "stage": str(stage),
        "stage_sha256": file_sha256(stage),
        "source_candidate": {"file": str(candidate),
                             "sha256": request["expected_sha256"]},
    }


def open_project(request):
    import bpy
    from animation_author import load_author
    load_author(bpy, request["edition"])
    from animation_curves_workflow_addon import clip_project
    source = Path(request["file"]).resolve()
    if file_sha256(source) != request["expected_sha256"]:
        raise WorkerError("CONFLICT", "project SHA-256 differs before reopening")
    if (bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False) != {"FINISHED"}
            or not Path(bpy.data.filepath).samefile(source)):
        raise WorkerError("VALIDATION_FAILED", "could not reopen the exact saved project")
    scene = bpy.data.scenes.get(request["context"]["scene"])
    layer = (scene.view_layers.get(request["context"]["view_layer"])
             if scene is not None else None)
    if scene is None or layer is None:
        raise WorkerError("NOT_FOUND", "requested scene or view layer is missing")
    if bpy.context.window is not None and bpy.context.window.scene != scene:
        bpy.context.window.scene = scene
    return bpy, clip_project, source, scene, layer


def read_only_witness(scene):
    from animation_workflow import state_fingerprint

    return state_fingerprint(), scene.frame_current, scene.frame_subframe


def unchanged_witness(scene, before, action):
    if read_only_witness(scene) != before:
        raise WorkerError("VALIDATION_FAILED", f"Project {action} changed Actions, records or playhead")


def project_pose_runtime(bpy, request):
    if not is_supported_host(bpy.app.version):
        return nullcontext()
    from animation_pose_handoff import observed_pose_runtime
    from animation_curves_workflow_addon import pose_gate
    from animation_curves_workflow_addon.windows_owned import runtime_identity

    if runtime_identity(bpy)["sha256"] != request["runtime"]["blender_sha256"]:
        raise WorkerError("CONFLICT", "Project pose gate or Blender build identity changed")
    return observed_pose_runtime(pose_gate, True)


def require_current_contacts(bpy, review):
    if is_supported_host(bpy.app.version):
        unresolved = [{"event_id": event["event_id"], "reason": event["status_error"]}
                      for event in review["events"] if not event["contact_known"]]
        if unresolved:
            labels = ", ".join(event["event_id"] for event in unresolved)
            raise WorkerError("CONFLICT", "Project event bindings do not resolve on the selected Blender: " + labels,
                              {"unresolved_events": unresolved})


def run_inspect(request, project, job):
    from animation_workflow import _resource_closure

    bpy, clip_project, source, scene, layer = project
    before = read_only_witness(scene)
    closure = _resource_closure(request)
    inspection = context_call(bpy, scene, layer, clip_project.inspect, request["manifest"])
    unchanged_witness(scene, before, "inspect")
    if file_sha256(source) != request["expected_sha256"]:
        raise WorkerError("CONFLICT", "source changed during inspection")
    return {"inspection": inspection, "resources": closure, "worker_pid": os.getpid(),
            "checks": {"read_only": "passed", "record_unchanged": "passed", "source_sha256": "passed",
                       "resources": "closed_by_actual_scan"}}


def run_review(request, project, job):
    bpy, clip_project, source, scene, layer = project
    edition = request["edition"]
    manifest = clip_project.contract.normalize_request(request["manifest"])
    if manifest["action"] not in RUN_ACTIONS:
        raise WorkerError("INVALID_REQUEST",
                          "review requires a mutating project action")
    before_state = read_only_witness(scene) if edition == "0.5.0" else None
    review = context_call(bpy, scene, layer, clip_project.review, manifest)
    require_current_contacts(bpy, review)
    window = key_window_for(manifest)
    before = compact_probe(probe(bpy, clip_project, scene, layer, None, window, edition=edition))
    if before_state is not None:
        unchanged_witness(scene, before_state, "review")
    declared_resources = request.get("resources", [])
    blend_resources = resource_manifest(bpy)
    all_resources = resource_manifest(bpy, declared_resources)
    plan = {
        "schema": PLAN_SCHEMA,
        "action": manifest["action"],
        "source": {"file": str(source), "sha256": request["expected_sha256"]},
        "context": dict(manifest["context"]),
        "manifest": request["manifest"],
        "normalized": manifest,
        "timing": timing(scene),
        "key_window": window,
        "review_digest": stable_review_digest(review),
        "addon_review_sha256": review.get("review_sha256"),
        "review_sha256": review["review_sha256"],
        "resources": declared_resources,
        "blend_resources": blend_resources,
        "resource_manifest": all_resources,
        "review_summary": {
            "action": review.get("action"),
            "book": review.get("book"),
            "book_raw_sha256": review.get("book_raw_sha256"),
            "counts": review.get("counts"),
            "relations": review.get("relations"),
            "proposed_book": review.get("proposed_book"),
            "proposed_event": review.get("proposed_event"),
            "relation_checks": review.get("relation_checks"),
            "geometry": review.get("geometry"),
            "review_sha256": review.get("review_sha256"),
        },
        "before": before,
    }
    if edition == "0.5.0":
        plan["runtime"] = request["runtime"]
    return {"plan": plan, "review_summary": plan["review_summary"],
            "review": review}


def run_apply(request, project, job):
    from animation_workflow import _resource_closure, animation_fingerprint

    bpy, clip_project, source, scene, layer = project
    plan = request["plan"]
    if plan["runtime"] != request["runtime"]:
        raise WorkerError("CONFLICT", "project plan author differs from the worker launch")
    stage = Path(request["stage"])
    if os.path.lexists(stage):
        raise WorkerError("CONFLICT", "project stage already exists")

    def checkpoint():
        if (job.parent / "cancel.request").is_file():
            raise WorkerError("CANCELLED", "project-events cancelled")

    closure = _resource_closure(request)
    animation_before = animation_fingerprint()
    before_state = read_only_witness(scene)
    reviewed = context_call(bpy, scene, layer, clip_project.review, request["manifest"])
    require_current_contacts(bpy, reviewed)
    unchanged_witness(scene, before_state, "review")
    if stable_review_digest(reviewed) != plan["review_digest"]:
        raise WorkerError("CONFLICT", "native apply review differs from the approved plan")
    checkpoint()
    execution = context_call(bpy, scene, layer, clip_project.execute, reviewed, checkpoint=checkpoint)
    checkpoint()
    animation_after = animation_fingerprint()
    if (animation_before != animation_after) != (plan["action"] == "retime"):
        raise WorkerError("VALIDATION_FAILED", "unexpected native Action changes for " + plan["action"])
    if _resource_closure(request) != closure:
        raise WorkerError("VALIDATION_FAILED", "project edit changed external resources or static audio")
    after_state = read_only_witness(scene)
    after = compact_probe(probe(bpy, clip_project, scene, layer, None, plan["key_window"], edition=request["edition"]))
    unchanged_witness(scene, after_state, "inspection")
    checks = verify_change(plan, plan["before"], after)
    if file_sha256(source) != request["expected_sha256"]:
        raise WorkerError("CONFLICT", "source changed during project apply")
    bpy.context.preferences.filepaths.save_version = 0
    if bpy.ops.wm.save_as_mainfile(filepath=str(stage), copy=True, relative_remap=True,
                                   check_existing=False) != {"FINISHED"}:
        raise WorkerError("IO_ERROR", "could not save the reviewed project stage")
    return {"worker_pid": os.getpid(), "stage_sha256": file_sha256(stage), "execution": execution,
            "animation": animation_after, "resources": closure, "review_digest": plan["review_digest"],
            "checks": checks}


# action: PF-05 verify against current recompute and staged resources.
def run_verify(request, project, job):
    bpy, clip_project, candidate, scene, layer = project
    plan = request["plan"]
    window = plan.get("key_window")
    edition = request["edition"]
    before_state = read_only_witness(scene) if edition == "0.5.0" else None
    after = compact_probe(probe(bpy, clip_project, scene, layer, None, window, edition=edition))
    checks = verify_change(plan, plan["before"], after)
    expected_parent = request.get("expected_parent")
    if expected_parent is not None:
        actual_parent = Path(bpy.data.filepath).resolve().parent
        if actual_parent != Path(expected_parent).resolve():
            raise WorkerError(
                "CONFLICT",
                "staged candidate was not reopened in the final parent directory",
                {"expected": str(expected_parent), "actual": str(actual_parent)})
    resources = compare_resources(plan["blend_resources"],
                                  resource_manifest(bpy))
    report = {"status": "passed", "checks": checks, "after": after,
            "resources": resources,
            "candidate": {"file": str(candidate),
                          "sha256": request["expected_sha256"]}}
    if before_state is not None:
        from animation_workflow import _resource_closure, animation_fingerprint

        unchanged_witness(scene, before_state, "cold inspection")
        if animation_fingerprint() != request["native_animation"] or _resource_closure(request) != request["native_resources"]:
            raise WorkerError("VALIDATION_FAILED", "native Action, resources or static audio differ on cold reopen")
        report["worker_pid"] = os.getpid()
    return report


def run(phase, request_path):
    request = read_json(request_path)
    if phase == "stage":
        return run_stage(request)
    operation = {"review": run_review, "inspect": run_inspect, "apply": run_apply, "verify": run_verify}.get(phase)
    if operation is None:
        raise WorkerError("INVALID_REQUEST", "unknown worker phase: " + str(phase))
    project = open_project(request)
    with project_pose_runtime(project[0], request):
        return operation(request, project, Path(request_path).resolve().parent)


def main():
    phase = sys.argv[sys.argv.index("--") + 1]
    request_path = Path(sys.argv[sys.argv.index("--") + 2]).resolve()
    try:
        response = {"ok": True, "data": run(phase, request_path)}
    except Exception as error:
        response = {"ok": False, "error": fail(error),
                    "traceback": traceback.format_exc()}
    write_json(request_path.parent / (phase + "-result.json"), response)


main()
