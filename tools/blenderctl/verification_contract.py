# SPDX-License-Identifier: GPL-3.0-or-later
"""Declarative, bounded validation profile; never evaluates Python from a request."""
import math
import re
from protocol import Failure


def require(ok, message):
    if not ok:
        raise Failure("INVALID_REQUEST", message)


def fields(obj, allowed, required=()):
    require(isinstance(obj, dict) and not set(obj) - set(allowed) and set(required) <= set(obj), "Invalid validation profile fields")


def selector(obj):
    fields(obj, ("type", "name", "library", "asset_id"))
    require(set(obj) in ({"asset_id"}, {"type", "name", "library"}), "Requires exact selector")
    require(all(isinstance(v, str) and v and "\0" not in v for k, v in obj.items() if k != "library"), "Invalid selector")
    require(obj.get("library") is None or isinstance(obj["library"], str), "Invalid library")


def validate_profile(p):
    fields(p, ("timeline", "hash_resources", "resources", "evaluate", "render", "asset_records", "asset_index", "catalogs", "require_complete", "closure"))
    if 'closure' in p:
        from dependency_contract import normalize_closure
        p['closure']=normalize_closure(p['closure'])
    if "asset_index" in p:
        require("asset_records" not in p, "Choose asset_index or embedded asset_records")
        fields(p["asset_index"], ("file", "expected_sha256"), ("file", "expected_sha256"))
        file_input(p["asset_index"])
    for flag in ("hash_resources", "render", "require_complete"):
        if flag in p:
            require(type(p[flag]) is bool, "Profile flags must be boolean")
    if "timeline" in p:
        t = p["timeline"]
        fields(t, ("scene", "start", "end", "step"), ("scene", "start", "end"))
        require(isinstance(t["scene"], str) and bool(t["scene"]), "Requires scene name")
        require(all(type(t.get(k, 1)) is int for k in ("start", "end", "step")), "Frames must be integers")
        require(-1048574 <= t["start"] <= t["end"] <= 1048574 and t.get("step", 1) > 0, "Invalid frame range")
        require(len(range(t["start"], t["end"] + 1, t.get("step", 1))) <= 1000, "At most 1000 sampled frames")
    for name in ("resources", "evaluate", "asset_records", "catalogs"):
        if name in p:
            require(isinstance(p[name], list) and len(p[name]) <= 1000, "Profile lists limited to 1000")
    for r in p.get("resources", []):
        fields(r, ("owner", "adapter", "property", "kind", "files"), ("owner", "adapter", "property", "kind", "files"))
        selector(r["owner"])
        require(r["adapter"] == "custom-property-files-v1" and r["kind"] in ("plugin", "cache"), "Unsupported resource adapter")
        require(isinstance(r["property"], str) and bool(r["property"]), "Requires custom property")
        require(isinstance(r["files"], list) and 1 <= len(r["files"]) <= 1000, "Requires 1..1000 declared files")
        for f in r["files"]:
            fields(f, ("path", "frame", "sha256"), ("path",))
            require(isinstance(f["path"], str) and bool(f["path"]) and "\0" not in f["path"], "Invalid resource path")
            if "frame" in f:
                require(type(f["frame"]) is int, "Resource frame must be integer")
            if "sha256" in f:
                require(isinstance(f["sha256"], str) and bool(re.fullmatch("[0-9a-f]{64}", f["sha256"])), "Invalid resource hash")
    for e in p.get("evaluate", []):
        fields(e, ("selector", "expect"), ("selector",))
        selector(e["selector"])
        require("timeline" in p, "Evaluation requires explicit timeline")
        require(isinstance(e.get("expect", []), list) and len(e.get("expect", [])) <= 1000, "Invalid expectations")
        for ex in e.get("expect", []):
            fields(ex, ("frame", "vertices", "location", "bounds", "tolerance", "bone", "bone_matrix"), ("frame",))
            require(type(ex["frame"]) is int and ex["frame"] in range(p["timeline"]["start"], p["timeline"]["end"] + 1, p["timeline"].get("step", 1)), "Expected frame not sampled")
            if "vertices" in ex:
                require(type(ex["vertices"]) is int and ex["vertices"] >= 0, "Invalid vertices")
            for k, length in (("location", 3), ("bounds", 6), ("bone_matrix", 16)):
                if k in ex:
                    require(isinstance(ex[k], list) and len(ex[k]) == length and all(type(v) in (int, float) and math.isfinite(v) for v in ex[k]), "Invalid expected numeric vector")
            tol = ex.get("tolerance", 1e-5)
            require(type(tol) in (float, int) and math.isfinite(tol) and 0 <= tol <= 1, "Invalid tolerance")
            require(("bone" in ex) == ("bone_matrix" in ex), "Bone expectation requires bone and matrix")
            if "bone" in ex:
                require(isinstance(ex["bone"], str), "Invalid bone")
    for c in p.get("catalogs", []):
        fields(c, ("library_root", "text", "file", "expected_sha256"), ("library_root",))
        require(isinstance(c["library_root"], str) and (("text" in c) != ("file" in c)), "Choose Catalog text or file")
        if "file" in c:
            file_input(c)
        else:
            require(isinstance(c["text"], str) and "expected_sha256" not in c, "Invalid Catalog text")
    if p.get("evaluate"):
        t = p["timeline"]
        require(len(p["evaluate"]) * len(range(t["start"], t["end"] + 1, t.get("step", 1))) <= 10000, "At most 10000 object/frame samples")
    return p


def file_input(spec):
    from pathlib import Path
    require(isinstance(spec.get("file"), str) and "\0" not in spec["file"] and Path(spec["file"]).is_absolute(), "Index/Catalog file must be absolute")
    require(isinstance(spec.get("expected_sha256"), str) and bool(re.fullmatch("[0-9a-f]{64}", spec["expected_sha256"])), "Index/Catalog requires expected_sha256")
    if not Path(spec["file"]).is_file():
        raise Failure("NOT_FOUND", "Index/Catalog file not found")
    spec["file"] = str(Path(spec["file"]).resolve())
