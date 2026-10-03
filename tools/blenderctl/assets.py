# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit asset edits on disposable candidates; sources and other assets stay intact."""
import json
import copy
import hashlib
from array import array
import os
from pathlib import Path
import uuid

import bpy
from inspection import all_ids, identity, key, snapshot, compare, select, validate_snapshot, record
from protocol import Failure, atomic_json, digest, read_json
from verification import resolve

SUPPORTED = {"Object", "Collection", "Material", "GeometryNodeTree", "ShaderNodeTree", "CompositorNodeTree", "Action", "World"}
ARCHIVE = "blenderctl_asset_archive_v1"


def previews():
    result = {}
    for item in all_ids():
        preview = item.preview
        if preview and all(preview.image_size):
            result[key(identity(item))] = {'size':list(preview.image_size), 'sha256':hashlib.sha256(array('i',preview.image_pixels).tobytes()).hexdigest()}
    return result


def prepare(params, job):
    source = snapshot(params["file"])
    atomic_json(job / "before.snapshot.json", source)
    manifest = params["manifest"]
    operation = manifest["operation"]
    items = list(all_ids())
    before_previews = previews()
    requested_previews = {}
    selected = [(resolve(items, t["selector"]), t) for t in manifest["targets"]]
    selected_keys = {key(identity(i)) for i, _ in selected}
    if len(selected_keys) != len(selected):
        raise Failure("CONFLICT", "Duplicate target")
    if any(i.library or i.override_library or i.bl_rna.identifier not in SUPPORTED for i, _ in selected):
        raise Failure("UNSUPPORTED", "Only supported local, non-override asset data-blocks can be edited")
    source_version = list(bpy.data.version)
    conversion = tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2])
    if conversion and not manifest.get('allow_version_conversion',False):
        raise Failure("UNSUPPORTED", "Asset candidates require same Blender major/minor")
    if any(f["code"] in {"INVALID_ASSET_ID", "DUPLICATE_ASSET_ID"} for f in validate_snapshot(source)["findings"]):
        raise Failure("CONFLICT", "Invalid or duplicate persistent IDs must be resolved first")
    if operation == "remove":
        adjacency = {}
        for edge in source["edges"]:
            adjacency.setdefault(key(edge["user"]), set()).add(key(edge["target"]))
        other_assets = {key(identity(i)) for i in items if i.asset_data and key(identity(i)) not in selected_keys}
        queue, visited = list(other_assets), set(other_assets)
        while queue:
            for target in adjacency.get(queue.pop(), ()):
                if target in selected_keys:
                    raise Failure("CONFLICT", "Removal would affect another marked asset; select the complete intended asset boundary")
                if target not in visited:
                    visited.add(target)
                    queue.append(target)
        for item, _ in selected:
            for user in bpy.data.user_map().get(item, ()):
                if key(identity(user)) not in selected_keys and not isinstance(user, (bpy.types.Scene, bpy.types.Collection)):
                    raise Failure("CONFLICT", "Removal would break a non-target data-block reference")
    changes = []
    for item, spec in selected:
        before = next(b for b in source["datablocks"] if key({k:b[k] for k in ("type","name","library")}) == key(identity(item)))
        if operation in {"unmark", "quarantine", "remove"} and not item.asset_data:
            raise Failure("CONFLICT", "Target is not a marked asset")
        if operation == "restore":
            if item.asset_data or ARCHIVE not in item:
                raise Failure("CONFLICT", "Restore requires an unmarked asset with saved metadata")
            saved = json.loads(item[ARCHIVE])
            if saved.get("asset_id") != item.get("asset_id") or not isinstance(saved.get("asset"), dict):
                raise Failure("CONFLICT", "Archived metadata does not belong to this ID")
            item.asset_mark()
            a = saved["asset"]
            item.asset_data.description, item.asset_data.author, item.asset_data.catalog_id = a["description"], a["author"], a["catalog_id"]
            for tag in a["tags"]:
                item.asset_data.tags.new(tag, skip_if_exists=True)
            item.use_fake_user = saved.get('fake_user', True)
            del item[ARCHIVE]
        elif operation in {"extract", "edit"}:
            if not item.asset_data:
                item.asset_mark()
            for name in ("description", "author", "catalog_id"):
                if name in spec:
                    setattr(item.asset_data, name, spec[name])
                    if getattr(item.asset_data,name) != spec[name]:
                        raise Failure('CONFLICT','Blender truncated or changed requested metadata: '+name)
            if "tags" in spec:
                for tag in list(item.asset_data.tags):
                    item.asset_data.tags.remove(tag)
                for tag in spec["tags"]:
                    item.asset_data.tags.new(tag, skip_if_exists=True)
                if sorted(t.name for t in item.asset_data.tags) != sorted(spec['tags']):
                    raise Failure('CONFLICT','Blender truncated or changed requested tags')
            if "name" in spec and spec["name"] != item.name:
                if any(i != item and not i.library and i.id_type == item.id_type and i.name == spec["name"] for i in items):
                    raise Failure("CONFLICT", "Name collides with another data-block; no automatic suffixing")
                item.name = spec["name"]
                if item.name != spec["name"]:
                    raise Failure("CONFLICT", "Blender could not preserve the requested exact name")
            if 'preview' in spec:
                with bpy.context.temp_override(id=item):
                    outcome = bpy.ops.ed.lib_id_load_custom_preview(filepath=spec['preview']['file'])
                loaded = previews().get(key(identity(item)))
                if outcome != {'FINISHED'} or not loaded:
                    raise Failure('VALIDATION_FAILED', 'Custom preview was not loaded')
                requested_previews[key(identity(item))] = loaded
        if operation != "remove":
            item["asset_id"] = str(uuid.UUID(item["asset_id"])) if item.get("asset_id") else str(uuid.uuid4())
        if operation in {"unmark", "quarantine"}:
            item[ARCHIVE] = json.dumps({"asset_id": item["asset_id"], "asset": before["asset"], "fake_user":before['fake_user'], "operation": operation}, ensure_ascii=False)
            item.asset_clear()
            item.use_fake_user = True
        changes.append({"before": before, "after_identity": identity(item), "asset_id": item.get("asset_id"), "operation": operation})
    retention = []
    if operation == "extract":
        if not manifest.get("keep_dependency_assets", False):
            for item in items:
                if key(identity(item)) not in {key(identity(i)) for i, _ in selected} and item.asset_data:
                    item.asset_clear()
        export = {i for i, _ in selected}
        # Blender's library writer does not expand every weak ID reference (for
        # example an object's parent). Include the observed dependency closure.
        by_identity={key(identity(i)):i for i in items}
        adjacency={}
        for edge in source['edges']:
            adjacency.setdefault(key(edge['user']),set()).add(key(edge['target']))
        pending=[key(identity(i)) for i in export];visited=set(pending)
        while pending:
            for target in adjacency.get(pending.pop(),()):
                if target in by_identity and target not in visited:
                    visited.add(target);pending.append(target);export.add(by_identity[target])
    elif operation == "remove":
        bpy.data.batch_remove(ids={i for i, _ in selected})
        export = set(all_ids())
    else:
        export = set(all_ids())
    if operation != "extract":
        for item in export:
            if item.users == 0 and not item.use_fake_user:
                item.use_fake_user = True
                retention.append(identity(item))
    candidate = job / "asset-candidate.blend"
    if candidate.exists():
        raise Failure("CONFLICT", "Candidate already exists")
    if operation == "extract":
        extraction_baseline = {key(identity(i)):record(i) for i in all_ids()}
        intermediate = job / 'extracted.stage'
        bpy.data.libraries.write(str(intermediate), export, path_remap="ABSOLUTE", fake_user=True, compress=True)
        bpy.ops.wm.open_mainfile(filepath=str(intermediate), load_ui=False, use_scripts=False)
        for item in all_ids():
            baseline = extraction_baseline.get(key(identity(item)))
            if baseline is not None:
                # libraries.write resets this Subsurf UI flag (ordinary saving
                # does not). Restore it instead of weakening content equality.
                if isinstance(item, bpy.types.Object):
                    prior = {m['name']: m for m in baseline['object']['modifiers']}
                    for modifier in item.modifiers:
                        saved = prior.get(modifier.name, {})
                        if modifier.type == 'SUBSURF' and 'open_advanced_panel' in saved:
                            modifier.open_advanced_panel = saved['open_advanced_panel']
                item.use_fake_user = baseline['fake_user']
                if item.users==0 and not item.use_fake_user:
                    item.use_fake_user=True;baseline['fake_user']=True;retention.append(identity(item))
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, relative_remap=False, compress=True)
    else:
        bpy.ops.file.make_paths_absolute()
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, relative_remap=False, compress=True)
    after = snapshot(str(candidate))
    after_previews = previews()
    for ident, expected_preview in requested_previews.items():
        if after_previews.get(ident) != expected_preview:
            raise Failure('VALIDATION_FAILED', 'Custom preview changed after independent reopen')
    atomic_json(job / "after.snapshot.json", after)
    difference = compare(source, after)
    if operation == 'extract':
        for block in after['datablocks']:
            ident = key({k:block[k] for k in ('type','name','library')})
            expected_block = extraction_baseline.get(ident)
            if expected_block is None and block['type']=='Scene':
                continue  # Blender supplies a default scene when opening a pure library.
            if block != expected_block:
                raise Failure('VALIDATION_FAILED', 'Extracted content changed: '+ident)
    if operation != "extract":
        # Content of surviving non-target blocks must be identical. Changed container
        # reference edges are explicitly allowed only for exact removal.
        before_index = {(b["type"], b["name"], b["library"]): b for b in source["datablocks"]}
        after_index = {(b["type"], b["name"], b["library"]): b for b in after["datablocks"]}
        renames = {key({k: c['before'][k] for k in ('type','name','library')}): c['after_identity'] for c in changes}
        def remap(value):
            if isinstance(value, dict):
                if set(value) == {'type','name','library'}:
                    return renames.get(key(value), value)
                return {k: remap(v) for k,v in value.items()}
            if isinstance(value, list):
                return [remap(v) for v in value]
            return value
        for ident, block in before_index.items():
            if key({k: block[k] for k in ("type","name","library")}) in selected_keys:
                continue
            expected = remap(copy.deepcopy(block))
            if {k:block[k] for k in ('type','name','library')} in retention:
                expected['fake_user'] = True
            if after_index.get(ident) != expected:
                raise Failure("VALIDATION_FAILED", "A non-target data-block changed during candidate save: " + str(ident))
            preview_key = key({k:block[k] for k in ('type','name','library')})
            if after_previews.get(preview_key) != before_previews.get(preview_key):
                raise Failure('VALIDATION_FAILED', 'A non-target preview changed')
        expected_edges = [remap(e) for e in source['edges'] if operation != 'remove' or not ({key(e['user']),key(e['target'])} & selected_keys)]
        if sorted(expected_edges, key=key) != sorted(after['edges'], key=key):
            raise Failure('VALIDATION_FAILED', 'Unexpected reference graph change')
    if operation == "extract" and not manifest.get("keep_dependency_assets", False):
        expected = {c["asset_id"] for c in changes}
        actual = {b["asset_id"] for b in after["datablocks"] if b["asset"]}
        if expected != actual:
            raise Failure("VALIDATION_FAILED", "Extracted asset marks differ from selected_only policy")
    result = {"candidate": str(candidate), "candidate_sha256": digest(candidate), "operation": operation,
              "changes": changes, "difference": difference, "asset_count": sum(bool(b["asset"]) for b in after["datablocks"]),
              "source_version": source_version, "reader_version":list(bpy.app.version), "version_converted":conversion,
              "previews": after_previews, "retained_orphans": retention, "source_unchanged": True, "publication": "not_run", "next_step": "project plan-files with kind=blend_exact; then independent validate"}
    atomic_json(job / "asset-change.json", result)
    return result


def index(params, job):
    from asset_validation import schema_errors
    from protocol import ROOT
    data = snapshot(params["file"])
    manifest = params["manifest"]
    marked = {b["asset_id"]: b for b in data["datablocks"] if b["asset"]}
    if None in marked or len(marked) != sum(bool(b["asset"]) for b in data["datablocks"]):
        raise Failure("CONFLICT", "Every active asset requires its own registered ID")
    specs = {s["asset_id"]: s for s in manifest["assets"]}
    if set(specs) != set(marked) or len(specs) != len(manifest["assets"]):
        raise Failure("CONFLICT", "Index manifest must cover each active asset exactly once")
    paths = sorted({p for d in data["dependencies"]["items"] if d["role"] != "output" and d["status"] not in {"packed","builtin","generated","no_external_path"}
                    for p in d.get("expected_files", [d["resolved_path"]]) if p and p not in d.get("packed_members", [])})
    records = []
    sha = digest(params["file"])
    definitions = {str(uuid.UUID(c["uuid"])): c for c in manifest["catalogs"]}
    for aid, block in marked.items():
        spec = specs[aid]
        catalog_id = block["asset"]["catalog_id"]
        if catalog_id not in definitions:
            raise Failure("VALIDATION_FAILED", "Asset Catalog UUID is not defined in the manifest")
        records.append({"schema_version":"1.0","asset_id":aid,"display_name":block["name"],"type":spec["type"],
                        "category":spec["category"],"tags":block["asset"]["tags"],"version":spec["version"],"lifecycle":"working",
                        "files":[{"path":manifest["output_file"],"path_base":None,"role":"primary","bytes":Path(params["file"]).stat().st_size,
                                  "sha256":sha,"modified_utc":None}],
                        "blender":{"datablocks":[{"type":block["type"],"name":block["name"]}],"validated_version":bpy.app.version_string},
                        "catalog":{"library_root":manifest["library_root"],"uuid":catalog_id,"path":definitions[catalog_id]["path"]},
                        "source":spec["source"],"dependencies":[{"path":p} for p in paths],"validation":{"status":"not_run"}})
    schema = read_json(ROOT / "docs/cli/schemas/asset-record.schema.json")
    for record in records:
        errors = list(schema_errors(record, schema))
        if errors:
            raise Failure("INVALID_REQUEST", "Invalid asset record fields: " + str(errors))
    catalog_text = "VERSION 1\n" + "".join(f"{uid}:{c['path']}:{c['simple_name']}\n" for uid,c in sorted(definitions.items()))
    catalog_path = job / "blender_assets.cats.txt"
    catalog_path.write_text(catalog_text, encoding="utf-8")
    from transactions import validate_document
    validate_document(catalog_path, "catalog")
    atomic_json(job / "asset-index.json", records)
    return {"index":str(job / "asset-index.json"),"catalog":str(catalog_path),"records":len(records),"blend_sha256":sha,
            "output_file":manifest["output_file"],"lifecycle":"working","publish_ready":False,
            "next_step":"commit candidate as blend_exact plus index/catalog with project plan-files, then validate final file/index/catalog"}
