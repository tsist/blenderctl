# SPDX-License-Identifier: GPL-3.0-or-later
"""Journaled file transactions; preserve originals before explicit replacement. Never delete."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from protocol import SCHEMA, Failure, atomic_json, digest, read_json
from filesystem import FileGuard, PathLocks


def event(name, context):
    """No-op instrumentation seam, replaced only by isolated acceptance test processes."""


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def normalize_item(item, mixed=False):
    keys = {"file", "output", "kind", "mode"} if mixed else {"file", "output"}
    if not isinstance(item, dict) or set(item)-({'dependencies'} if mixed else set()) != keys:
        raise Failure("INVALID_REQUEST", f"Each item requires exactly {sorted(keys)}")
    kind, mode = item.get("kind", "blend"), item.get("mode", "create")
    if kind not in ("blend", "blend_exact", "catalog", "catalog_repair", "json") or mode not in ("create", "replace"):
        raise Failure("INVALID_REQUEST", "kind must be blend/blend_exact/catalog/catalog_repair/json; mode must be create/replace")
    if kind=='catalog_repair' and mode!='replace':
        raise Failure('INVALID_REQUEST','Catalog repair requires an existing replacement target')
    out = {"kind": kind, "mode": mode} if mixed else {}
    for key in ("file", "output"):
        value = item[key]
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise Failure("INVALID_REQUEST", f"Invalid {key} path")
        path = Path(value)
        if any(part.endswith((" ", ".")) or (hasattr(os.path, "isreserved") and os.path.isreserved(part)) for part in path.parts if part not in (path.anchor, ".", "..")):
            raise Failure("INVALID_REQUEST", "Reserved or ambiguous Windows path")
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise Failure("CONFLICT", "File symlinks and junctions are not transaction targets or sources")
        if key == "output" and mode == "create" and os.path.lexists(path):
            raise Failure("CONFLICT", f"Output already exists: {path}")
        path = path.resolve()
        suffix = {"blend": ".blend", "blend_exact": ".blend", "catalog": ".txt", "catalog_repair": ".txt", "json": ".json"}[kind]
        if path.suffix.lower() != suffix:
            raise Failure("INVALID_REQUEST", f"{kind} source and output must end in {suffix}")
        if not path.parent.is_dir():
            raise Failure("NOT_FOUND", f"Parent directory does not exist: {path.parent}")
        if key == "file" and not path.is_file():
            raise Failure("NOT_FOUND", f"Source not found: {path}")
        if key == "output" and mode == "replace" and not path.is_file():
            raise Failure("NOT_FOUND", f"Replacement requires an existing regular file: {path}")
        out[key] = str(path)
    if os.path.normcase(out["file"]) == os.path.normcase(out["output"]):
        raise Failure("CONFLICT", "Source and target must differ")
    if 'dependencies' in item:
        from node_contract import FILE
        from scene_contract import array, validate
        validate(item['dependencies'],array(FILE,1,1000))
        out['dependencies']=[];seen=set()
        for dep in item['dependencies']:
            if not Path(dep['file']).is_absolute():raise Failure('INVALID_REQUEST','Transaction dependency paths must be absolute')
            name=str(Path(dep['file']).resolve());key=os.path.normcase(name)
            if key in seen:raise Failure('INVALID_REQUEST','Duplicate transaction dependency')
            seen.add(key);out['dependencies'].append({**dep,'file':name})
    return out


def locate(root, transaction_id):
    if not re.fullmatch("[0-9a-f]{32}", transaction_id):
        raise Failure("INVALID_REQUEST", "Invalid transaction ID")
    root = Path(root).resolve()
    directory = (root / transaction_id).resolve()
    if directory.parent != root:
        raise Failure("INVALID_REQUEST", "Transaction path leaves registry")
    if not directory.is_dir():
        raise Failure("NOT_FOUND", "Transaction not found")
    return directory


def persist(directory, state):
    state["updated_at"] = time.time()
    atomic_json(directory / "journal.json", state)


def load(directory):
    plan = read_json(directory / "plan.json")
    state = read_json(directory / "journal.json")
    if state.get("plan_hash") != canonical_hash(plan) or plan.get("transaction_id") != directory.name:
        raise Failure("CONFLICT", "Plan was changed or belongs to another transaction")
    if (plan.get("schema_version"), plan.get("operation")) not in (("1.0", "save_copy_new"), ("1.1", "file_set"), ("1.2", "file_set"), ("1.3", "file_set"), ("1.4", "file_set")):
        raise Failure("UNSUPPORTED", "Unsupported transaction plan")
    return plan, state


def create(items, root, launch, checkpoint, mixed=False):
    items = [normalize_item(i, mixed=mixed) for i in items]
    if not 1 <= len(items) <= 100:
        raise Failure("INVALID_REQUEST", "A plan requires 1..100 items")
    names = [os.path.normcase(i["output"]) for i in items]
    if len(set(names)) != len(names):
        raise Failure("CONFLICT", "Duplicate output paths")
    if set(names) & {os.path.normcase(i["file"]) for i in items}:
        raise Failure("CONFLICT", "A batch output may not replace any batch source")
    dependencies={}
    for item in items:
        for dep in item.get('dependencies',[]):
            key=os.path.normcase(dep['file'])
            if key in names:raise Failure('CONFLICT','A batch output may not replace an upstream dependency')
            if key in dependencies and dependencies[key]!=dep:raise Failure('CONFLICT','Conflicting dependency descriptors')
            dependencies[key]=dep
    dependency_ids=set()
    for dep in dependencies.values():
        with FileGuard(dep['file']) as guard:
            if guard.sha256(checkpoint)!=dep['expected_sha256']:raise Failure('CONFLICT','Transaction upstream dependency changed before planning: '+dep['file'])
            ident=guard.identity();dependency_ids.add((ident['volume'],ident['file_index']))
    directory = Path(root).resolve() / uuid.uuid4().hex
    for index, item in enumerate(items):
        checkpoint()
        if item.get("mode", "create") == "create" and os.path.lexists(item["output"]):
            raise Failure("CONFLICT", f"Output already exists: {item['output']}")
        with FileGuard(item["file"]) as source, FileGuard(Path(item["output"]).parent, directory=True) as parent:
            item.update(source_sha256=source.sha256(checkpoint), source_identity=source.identity(), source_bytes=Path(item["file"]).stat().st_size,
                        target_parent_identity=parent.identity(), target_before=None)
            if item.get("mode") == "replace":
                with FileGuard(item["output"]) as old:
                    item["target_before"] = {"output_sha256": old.sha256(checkpoint), "file_identity": old.identity()}
                    item["target_backup"] = str(Path(item["output"]).parent / f".blenderctl-{directory.name}-{index}.original")
                    if source.identity() == old.identity():
                        raise Failure("CONFLICT", "Source and replacement target are the same file object")
                    if item.get("kind") in ("catalog", "catalog_repair"):
                        before = repair_baseline(item['output']) if item['kind']=='catalog_repair' else validate_document(item["output"], "catalog")
                        after = validate_document(item["file"], "catalog")
                        if not set(before) <= set(after):
                            raise Failure("CONFLICT", "Catalog replacement must preserve every existing UUID")
                        item["catalog_mapping"] = {key: {"before": before.get(key), "after": val} for key, val in after.items()}
                        if item['kind']=='catalog_repair' and (set(before)!=set(after) or any(before[k]['path']!=after[k]['path'] or (before[k]['simple_name'] is not None and before[k]['simple_name']!=after[k]['simple_name']) for k in before)):
                            raise Failure('CONFLICT','Catalog repair preserves every UUID/path and every valid display name')
            if item.get("kind", "blend") not in ("blend", "blend_exact"):
                validate_document(item["file"], item["kind"])
    source_ids = {(i["source_identity"]["volume"], i["source_identity"]["file_index"]) for i in items}
    old_ids = [(i["target_before"]["file_identity"]["volume"], i["target_before"]["file_identity"]["file_index"]) for i in items if i["target_before"]]
    if source_ids.intersection(old_ids) or len(old_ids) != len(set(old_ids)):
        raise Failure("CONFLICT", "Source/target file aliases and duplicate replacement identities are forbidden")
    if dependency_ids.intersection(old_ids):raise Failure('CONFLICT','Replacement target aliases an upstream dependency')
    directory.mkdir(parents=True, exist_ok=False)
    plan = {"schema_version": "1.2" if any(i.get("kind") == "blend_exact" for i in items) else "1.1" if mixed else "1.0", "transaction_id": directory.name, "operation": "file_set" if mixed else "save_copy_new",
            "created_at": time.time(), "blender": launch["blender"], "items": items,
            "policy": {"overwrite": "preserve_then_publish" if mixed else False, "relative_paths": "remap_to_target_parent", "version": "same_major_minor_only",
                       "validation": "structural_not_full_functional", "purpose": "working_copy_not_asset_publication"}}
    if plan['schema_version'] == '1.2':
        plan['policy']['relative_paths'] = 'per_item_remap_or_exact_resolved_inputs'
    if any(i.get('kind')=='catalog_repair' for i in items):
        plan['schema_version']='1.3'
        plan['policy']['relative_paths']='per_item_remap_or_exact_resolved_inputs'
    if dependencies:
        plan['schema_version']='1.4'
        plan['policy']['upstream_dependencies']='hash_locked_through_commit'
    atomic_json(directory / "plan.json", plan)
    state = {"schema_version": "1.0", "transaction_id": directory.name, "plan_hash": canonical_hash(plan),
             "phase": "planned", "intent": "apply", "items": [{"state": "planned", "attempts": []} for _ in items]}
    persist(directory, state)
    return {"transaction_id": directory.name, "phase": "planned", "dry_run": True, "plan": plan,
            "plan_hash": state["plan_hash"], "transaction_directory": str(directory)}


def repair_baseline(path):
    lines=[l.strip() for l in Path(path).read_bytes().splitlines() if l.strip() and not l.lstrip().startswith(b'#')]
    if not lines or lines.pop(0)!=b'VERSION 1':
        raise Failure('VALIDATION_FAILED','Repair requires a valid ASCII Catalog header')
    result={}
    for line in lines:
        try:
            uid,path,name=line.split(b':');uid=uid.decode('ascii');path=path.decode('utf-8')
            if str(uuid.UUID(uid))!=uid.lower() or uid.lower() in result or not path or any(p in ('','.','..') for p in path.split('/')) or '\\' in path:
                raise ValueError()
            try: name=name.decode('utf-8')
            except UnicodeError:name=None
            result[uid.lower()]={'path':path,'simple_name':name}
        except (ValueError,UnicodeError):
            raise Failure('VALIDATION_FAILED','Only malformed display names are repairable; UUID and path must be intact')
    return result


def validate_document(path, kind):
    """Transport-level validation only; no claim of asset index/dependency closure."""
    if kind == "json":
        value = read_json(path)
        try:
            canonical_hash(value)  # reject NaN/Infinity as well as duplicate keys
        except ValueError as exc:
            raise Failure("VALIDATION_FAILED", "JSON document contains a non-finite number") from exc
        if not isinstance(value, (dict, list)):
            raise Failure("VALIDATION_FAILED", "JSON document must be an object or array")
        return value
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        lines = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        if not lines or lines.pop(0) != "VERSION 1":
            raise ValueError("Expected VERSION 1")
        entries = {}
        paths = set()
        for line in lines:
            ident, path, name = line.split(":")
            parsed = str(uuid.UUID(ident))
            if parsed != ident.lower() or parsed in entries or not path or not name:
                raise ValueError("Invalid or duplicate Catalog UUID/path")
            if any(part in ("", ".", "..") for part in path.split("/")) or "\\" in path:
                raise ValueError("Invalid Catalog path")
            entries[parsed] = {"path": path, "simple_name": name}
            paths.add(path)
        return entries
    except (ValueError, UnicodeError) as exc:
        raise Failure("VALIDATION_FAILED", f"Invalid UTF-8 Catalog: {exc}") from exc


def normalize_snapshot(snapshot):
    """Compare scoped data and resolved dependencies, excluding volatile UI and path spelling."""
    base = Path(snapshot["file"]).parent
    ui_types = {"Screen", "WorkSpace", "WindowManager"}
    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            result = {}
            for key, val in value.items():
                if key == "raw_path":
                    continue
                if key == "library" and isinstance(val, str):
                    val = str((base / val[2:]).resolve()) if val.startswith("//") else os.path.normpath(val)
                    val = os.path.normcase(val)
                if key == "resolved_path" and isinstance(val, str):
                    val = os.path.normcase(os.path.normpath(val))
                result[key] = clean(val)
            return result
        return value
    blocks = [clean(i) for i in snapshot["datablocks"] if i["type"] not in ui_types]
    refs = []
    for ref in snapshot["references"]:
        if ref["target"]["type"] in ui_types:
            continue
        users = [u for u in ref["users"] if u["type"] not in ui_types]
        if users:
            refs.append(clean({"target": ref["target"], "users": users}))
    order = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)
    return {"datablocks": sorted(blocks, key=order), "references": sorted(refs, key=order),
            "dependencies": sorted(clean(snapshot["dependencies"]), key=order),
            **{key: snapshot[key] for key in ("scene_count", "object_count", "asset_count")}}


def worker(directory, launch, request, checkpoint, record):
    import runner
    job = runner.prepare(request, blender=launch["blender"], root=directory / "workers",
                         timeout=launch["timeout_seconds"], internal=request["command"].startswith("_"))
    record.setdefault("worker_jobs", []).append(job.name)
    result, code = runner.execute(job, parent_checkpoint=checkpoint)
    if code:
        raise Failure(result["error"]["code"], result["error"]["message"])
    return result["data"]


def check_owned(path, record, checkpoint, *, renameable=False):
    try:
        guard = FileGuard(path, renameable=renameable)
    except Failure as exc:
        if exc.code == "NOT_FOUND":
            raise Failure("CONFLICT", f"Expected transaction file disappeared: {path}") from exc
        raise
    try:
        if guard.identity() != record["file_identity"] or guard.sha256(checkpoint) != record["output_sha256"]:
            raise Failure("CONFLICT", f"File is not this transaction's unchanged output: {path}")
        return guard
    except BaseException:
        guard.close()
        raise


def reconcile(plan, state, checkpoint):
    """Recognize publish/rollback that finished just before a supervisor or journal failure."""
    for item, record in zip(plan["items"], state["items"]):
        checkpoint()
        if item["target_before"]:
            reconcile_replacement(item, record, state, checkpoint)
            continue
        if record.get("output_sha256") and os.path.lexists(item["output"]):
            if os.path.lexists(record["stage_path"]):
                raise Failure("CONFLICT", "Both target and staging exist; cannot attribute a rename to this transaction")
            with check_owned(item["output"], record, checkpoint):
                record["state"] = "committed"
        elif record.get("rollback_path") and os.path.lexists(record["rollback_path"]):
            with check_owned(record["rollback_path"], record, checkpoint):
                record["state"] = "rolled_back"
        elif record["state"] == "committed":
            raise Failure("CONFLICT", f"Committed target disappeared: {item['output']}")
        elif os.path.lexists(item["output"]):
            raise Failure("CONFLICT", f"Unowned target already exists: {item['output']}")


def reconcile_replacement(item, record, state, checkpoint):
    backup_exists = os.path.lexists(item["target_backup"])
    if backup_exists:
        with check_owned(item["target_backup"], item["target_before"], checkpoint):
            pass
    else:
        if record.get("old_preserved") and not (state["intent"] == "rollback" and record.get("restore_started")):
            raise Failure("CONFLICT", "Preserved original disappeared")
        with check_owned(item["output"], item["target_before"], checkpoint):
            pass
    record["old_preserved"] = backup_exists
    if not record.get("output_sha256"):
        if backup_exists:
            raise Failure("CONFLICT", "Original moved without validated staging evidence")
        return
    candidates = [("ready", record["stage_path"])]
    if backup_exists:
        candidates.append(("committed", item["output"]))
    if record.get("rollback_path"):
        candidates.append(("archived", record["rollback_path"]))
    found = [(label, path) for label, path in candidates if os.path.lexists(path)]
    if len(found) != 1:
        raise Failure("CONFLICT", "Expected exactly one owned new version (stage/target/rollback)")
    label, path = found[0]
    with check_owned(path, record, checkpoint):
        pass
    if label == "archived":
        if state["intent"] != "rollback":
            raise Failure("CONFLICT", "Archived version without rollback intent")
        label = "archived" if backup_exists else "rolled_back"
    record["state"] = label


def make_stage(directory, launch, item, record, source, state, checkpoint):
    attempt = uuid.uuid4().hex
    kind = item.get("kind", "blend")
    stage = Path(item["output"]).parent / (".blenderctl-" + attempt + (".blend" if kind == "blend" else ".stage"))
    snapshot = directory / ("source-" + attempt + ".snapshot")
    record["attempts"].append({"stage": str(stage), "snapshot": str(snapshot)})
    record.update(state="staging", stage_path=str(stage), snapshot=str(snapshot))
    persist(directory, state)  # log paths before creating anything
    event("before_snapshot", {"item": item, "record": record})
    with snapshot.open("xb") as out:
        for chunk in source.chunks(checkpoint):
            out.write(chunk)
            event("snapshot_chunk", {"path": str(snapshot)})
        out.flush()
        os.fsync(out.fileno())
    if digest(snapshot, checkpoint) != item["source_sha256"]:
        raise Failure("VALIDATION_FAILED", "Snapshot hash mismatch")
    if kind != "blend":
        with stage.open("xb") as out:
            for chunk in source.chunks(checkpoint):
                out.write(chunk)
                event("stage_chunk", {"item": item, "record": record})
            out.flush()
            os.fsync(out.fileno())
        event("after_save", {"item": item, "record": record})
        with FileGuard(stage) as guard:
            staged_hash = guard.sha256(checkpoint)
            if staged_hash != item["source_sha256"]:
                raise Failure("VALIDATION_FAILED", "Document copy hash mismatch")
            if kind == "blend_exact":
                before = worker(directory, launch, {"schema_version": SCHEMA, "command": "inspect", "params": {"file": item["file"]}}, checkpoint, record)
                after = worker(directory, launch, {"schema_version": SCHEMA, "command": "_inspect_staged", "params": {"file": str(stage)}}, checkpoint, record)
                baseline = directory / ("baseline-" + attempt + ".json")
                atomic_json(baseline, before)
                record["baseline"] = str(baseline)
                if normalize_snapshot(before) != normalize_snapshot(after):
                    raise Failure("VALIDATION_FAILED", "Exact byte relocation changes resolved references; prepare absolute/packed references first")
            else:
                validate_document(stage, kind)
            record.update(state="ready", output_sha256=staged_hash, file_identity=guard.identity(),
                          verification={"exact_bytes": "pass", "syntax": "pass", "asset_semantics": "not_run"})
        persist(directory, state)
        event("after_ready", {"item": item, "record": record})
        return
    request = {"schema_version": SCHEMA, "command": "_save_copy", "params": {"file": item["file"], "output": str(stage)}}
    data = worker(directory, launch, request, checkpoint, record)
    event("after_save", {"item": item, "record": record})
    baseline = directory / ("baseline-" + attempt + ".json")
    atomic_json(baseline, data["before"])
    record["baseline"] = str(baseline)
    with FileGuard(stage) as guard:
        staged_hash = guard.sha256(checkpoint)
        reopened = worker(directory, launch, {"schema_version": SCHEMA, "command": "inspect", "params": {"file": str(stage)}}, checkpoint, record)
        if normalize_snapshot(data["before"]) != normalize_snapshot(reopened):
            atomic_json(directory / ("mismatch-" + attempt + ".json"), {"before": normalize_snapshot(data["before"]), "after": normalize_snapshot(reopened)})
            raise Failure("VALIDATION_FAILED", "Saved copy differs from source structural snapshot")
        record.update(state="ready", output_sha256=staged_hash, file_identity=guard.identity(),
                      verification={"independent_reopen": "pass", "structural_comparison": "pass", "render": "not_run", "animation": "not_run"})
    persist(directory, state)  # complete recovery evidence BEFORE rename
    event("after_ready", {"item": item, "record": record})


def snapshot_original(directory, item, record, guard, state, checkpoint):
    # The immutable before-image is completed before the first target is moved.
    if record.get("target_snapshot"):
        if digest(record["target_snapshot"], checkpoint) != item["target_before"]["output_sha256"]:
            raise Failure("CONFLICT", "Original snapshot was modified")
        return
    path = directory / ("original-" + uuid.uuid4().hex + ".snapshot")
    record.setdefault("original_attempts", []).append(str(path))
    persist(directory, state)
    with path.open("xb") as out:
        for chunk in guard.chunks(checkpoint):
            out.write(chunk)
            event("original_snapshot_chunk", {"item": item, "record": record})
        out.flush()
        os.fsync(out.fileno())
    if digest(path, checkpoint) != item["target_before"]["output_sha256"]:
        raise Failure("VALIDATION_FAILED", "Original snapshot hash mismatch")
    record["target_snapshot"] = str(path)
    persist(directory, state)


def apply(directory, plan, state, launch, checkpoint):
    if state["phase"] == "rolled_back":
        raise Failure("CONFLICT", "Rolled back transaction is terminal; create a new plan")
    if launch["blender"] != plan["blender"]:
        raise Failure("CONFLICT", "Blender executable differs from the frozen plan")
    with ExitStack() as stack:
        # Freeze actual source files against other writers for the entire save/commit interval.
        dependency_guards={}
        for item in plan['items']:
            for dep in item.get('dependencies',[]):
                name=dep['file']
                if name not in dependency_guards:dependency_guards[name]=stack.enter_context(FileGuard(name))
                if dependency_guards[name].sha256(checkpoint)!=dep['expected_sha256']:raise Failure('CONFLICT','Transaction upstream dependency changed: '+name)
        if 'asset_scope' in plan:
            from library import acquire_inputs
            acquire_inputs(plan['asset_scope'],stack,checkpoint)
        sources = {}
        for item in plan["items"]:
            name = item["file"]
            if name not in sources:
                sources[name] = stack.enter_context(FileGuard(name))
            guard = sources[name]
            if guard.sha256(checkpoint) != item["source_sha256"] or guard.identity() != item["source_identity"]:
                raise Failure("CONFLICT", f"Source changed since planning: {name}")
        state.update(phase="staging", intent="apply")
        persist(directory, state)
        with ExitStack() as originals:
            for item, record in zip(plan["items"], state["items"]):
                if item["target_before"]:
                    path = item["target_backup"] if record.get("old_preserved") else item["output"]
                    old = originals.enter_context(check_owned(path, item["target_before"], checkpoint))
                    snapshot_original(directory, item, record, old, state, checkpoint)
            for item, record in zip(plan["items"], state["items"]):
                checkpoint()
                if record["state"] == "committed":
                    continue
                if record["state"] == "ready":
                    with check_owned(record["stage_path"], record, checkpoint):
                        pass
                else:
                    make_stage(directory, launch, item, record, sources[item["file"]], state, checkpoint)
        if 'asset_scope' in plan:
            from library import validate_transaction
            with ExitStack() as semantic_guards:
                for item,record in zip(plan['items'],state['items']):
                    semantic_guards.enter_context(check_owned(item['output'] if record['state']=='committed' else record['stage_path'],record,checkpoint))
                validate_transaction(directory,plan,state,launch,checkpoint)
        state["phase"] = "committing"
        persist(directory, state)
        with ExitStack() as committing:
            old_guards, new_guards = {}, {}
            # Revalidate all targets and all staged versions before the first rename.
            for index, (item, record) in enumerate(zip(plan["items"], state["items"])):
                if item["target_before"]:
                    path = item["target_backup"] if record.get("old_preserved") else item["output"]
                    old_guards[index] = committing.enter_context(check_owned(path, item["target_before"], checkpoint, renameable=True))
                    if not record.get("old_preserved") and os.path.lexists(item["target_backup"]):
                        raise Failure("CONFLICT", "Original preservation destination already exists")
                if record["state"] == "committed":
                    committing.enter_context(check_owned(item["output"], record, checkpoint))
                else:
                    new_guards[index] = committing.enter_context(check_owned(record["stage_path"], record, checkpoint, renameable=True))
                    if not item["target_before"] and os.path.lexists(item["output"]):
                        raise Failure("CONFLICT", "New target already exists")
            for index, guard in new_guards.items():
                item, record = plan["items"][index], state["items"][index]
                checkpoint()
                if item["target_before"] and not record.get("old_preserved"):
                    record["preserve_started"] = True
                    persist(directory, state)
                    event("before_preserve", {"item": item, "record": record})
                    old_guards[index].rename_new(item["target_backup"])
                    event("after_preserve", {"item": item, "record": record})
                    record["old_preserved"] = True
                    persist(directory, state)
                event("before_publish", {"item": item, "record": record})
                checkpoint()
                guard.rename_new(item["output"])
                event("after_publish", {"item": item, "record": record})
                record["state"] = "committed"
                persist(directory, state)
        # Reopen final paths; relative references must resolve exactly as in staging.
        state["phase"] = "verifying"
        persist(directory, state)
        with ExitStack() as verifying:
            for item, record in zip(plan["items"], state["items"]):
                verifying.enter_context(check_owned(item["output"], record, checkpoint))
                if item["target_before"]:
                    verifying.enter_context(check_owned(item["target_backup"], item["target_before"], checkpoint))
            for item, record in zip(plan["items"], state["items"]):
                if item.get("kind", "blend") in ("blend", "blend_exact"):
                    final = worker(directory, launch, {"schema_version": SCHEMA, "command": "inspect", "params": {"file": item["output"]}}, checkpoint, record)
                    if normalize_snapshot(final) != normalize_snapshot(read_json(record["baseline"])):
                        raise Failure("VALIDATION_FAILED", "Final target structural validation failed")
                else:
                    validate_document(item["output"], item["kind"])
                record["verification"]["final_path_reopen"] = "pass"
            if 'asset_scope' in plan:
                validate_transaction(directory,plan,state,launch,checkpoint,final=True)
            state["phase"] = "committed"
            state["last_error"] = None
            persist(directory, state)


def rollback(directory, plan, state, checkpoint):
    if state["phase"] == "rolled_back":
        return
    # Acquire and validate every output before moving the first one.
    with ExitStack() as stack:
        guards, originals = {}, {}
        for index, (item, record) in enumerate(zip(plan["items"], state["items"])):
            if item["target_before"]:
                path = item["target_backup"] if record.get("old_preserved") else item["output"]
                old = stack.enter_context(check_owned(path, item["target_before"], checkpoint, renameable=record.get("old_preserved", False)))
                if record.get("old_preserved"):
                    originals[index] = old
            if record["state"] in ("archived", "rolled_back") and record.get("rollback_path"):
                stack.enter_context(check_owned(record["rollback_path"], record, checkpoint))
            if record["state"] in ("committed", "ready"):
                owned_path = item["output"] if record["state"] == "committed" else record["stage_path"]
                guards[index] = stack.enter_context(check_owned(owned_path, record, checkpoint, renameable=True))
                record.setdefault("rollback_path", str(Path(item["output"]).parent / (".blenderctl-" + directory.name + f"-{index}.rollback")))
                if os.path.lexists(record["rollback_path"]):
                    raise Failure("CONFLICT", "Rollback destination already exists")
        state.update(phase="rolling_back", intent="rollback")
        persist(directory, state)
        for index in reversed(range(len(plan["items"]))):
            checkpoint()
            item, record = plan["items"][index], state["items"][index]
            if index in guards:
                guards[index].rename_new(record["rollback_path"])
                event("after_rollback_move", {"item": item, "record": record})
                record["state"] = "archived"
                persist(directory, state)
            if index in originals:
                record["restore_started"] = True
                persist(directory, state)
                event("before_restore", {"item": item, "record": record})
                originals[index].rename_new(item["output"])
                event("after_restore", {"item": item, "record": record})
                record["old_preserved"] = False
            record["state"] = "rolled_back"
            persist(directory, state)
        state["phase"] = "rolled_back"
        state["last_error"] = None
        persist(directory, state)


def run(request, launch, job, checkpoint):
    command = request["command"]
    params = request["params"]
    root = Path(launch["transactions_root"])
    if command=='asset.plan':
        from library import plan
        return plan(params['manifest'],launch,job,checkpoint)
    if command.startswith("project.plan_"):
        items = [params] if command == "project.plan_copy" else params["items"]
        for item in items:
            if any(Path(item["output"]).is_relative_to(p.resolve()) for p in (root, job.parent)):
                raise Failure("CONFLICT", "Output may not replace or populate transaction/job records")
        return create(items, root, launch, checkpoint, mixed=command == "project.plan_files")
    directory = locate(root, params["transaction_id"])
    if command == "transaction.status":
        plan, state = load(directory)
        return {"transaction_directory": str(directory), "plan": plan, "journal": state}
    # Lock transaction record FIRST, then all canonical source/output paths in stable order.
    with PathLocks([directory]):
        plan, state = load(directory)
        paths = [v for item in plan["items"] for v in (item["file"], item["output"])]
        with PathLocks(paths), ExitStack() as stack:
            parents = {}
            for item in plan["items"]:
                parent = str(Path(item["output"]).parent)
                if parent not in parents:
                    parents[parent] = stack.enter_context(FileGuard(parent, directory=True))
                if parents[parent].identity() != item["target_parent_identity"]:
                    raise Failure("CONFLICT", "Target parent directory was replaced")
            try:
                if command == "transaction.apply" and state["intent"] == "rollback":
                    raise Failure("CONFLICT", "Rollback intent is already durable; use recover or rollback")
                if state["phase"] == "rolled_back":
                    if command == "transaction.apply":
                        raise Failure("CONFLICT", "Rolled back transaction is terminal")
                    return {"transaction_directory": str(directory), "journal": state, "idempotent": True}
                reconcile(plan, state, checkpoint)
                persist(directory, state)
                event("after_reconcile", {"directory": str(directory), "state": state})
                if command == "transaction.rollback" or state["intent"] == "rollback":
                    rollback(directory, plan, state, checkpoint)
                elif state["phase"] == "committed":
                    return {"transaction_directory": str(directory), "journal": state, "idempotent": True}
                else:
                    apply(directory, plan, state, launch, checkpoint)
            except BaseException as exc:
                state["last_error"] = {"type": type(exc).__name__, "message": str(exc), "job_id": job.name}
                try:
                    persist(directory, state)
                except OSError:
                    pass  # preserve last durable intent; no mutation after journal failure
                raise
    return {"transaction_id": directory.name, "transaction_directory": str(directory), "journal": state}
