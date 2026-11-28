"""Look up production routes and their action-level evidence without opening Blender."""
import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import io
import json
import os
import sys
from pathlib import Path

from action_editions import HARD_SURFACE_LOCAL_EDITION, HARD_SURFACE_PREVIOUS_LOCAL_EDITION
from host_compat import is_supported_host
from protocol import Failure, VERSION

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "docs" / "cli" / "workflows"
LOCAL_HARD_SURFACE_TOOLS = frozenset({
    "align.reference", "mesh.snap_symmetry", "native.split_face", "native.loop_cut_slide", "native.flange",
    "surface_sweep",
})
LOCAL_HARD_SURFACE_VERSIONS = {
    HARD_SURFACE_PREVIOUS_LOCAL_EDITION: ("0.3.2", "0.3.4"),
    "本地发行 0.3.5 / CLI 0.91.0": ("0.3.4", "0.3.5"),
    "本地发行 0.3.6 / CLI 0.91.0": ("0.3.5", "0.3.6"),
    HARD_SURFACE_LOCAL_EDITION: ("0.3.6", "0.3.7"),
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_blender(value):
    """Resolve the launcher input without starting Blender."""
    try:
        binary = Path(value).resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"Blender executable not found: {value}") from None
    if not binary.is_file() or binary.suffix.lower() != ".exe":
        raise ValueError(f"Blender executable is not a Windows .exe file: {binary}")
    with binary.open("rb") as stream:
        header = stream.read(64)
        if len(header) == 64 and header.startswith(b"MZ"):
            stream.seek(int.from_bytes(header[60:64], "little"))
            if stream.read(4) == b"PE\0\0":
                return binary
    raise ValueError(f"Blender executable does not have a Windows PE header: {binary}")


def catalog_host(binary, version):
    # Catalog evidence is tied to the audited executable, including after relocation.
    pins = {"5.3.0": "6670c69fbb562c0fd1fae1e35f5413a90781cdf7d44af151ee4c9c3b82a38174"}
    if version not in pins:
        return None
    with binary.open("rb") as stream:
        return version if hashlib.file_digest(stream, "sha256").hexdigest() == pins[version] else None


def inherited_actions(product, rows):
    """Carry an edition's action definitions forward without carrying its run status."""
    editions = product.get("editions", [])
    inheritance = product.get("edition_inheritance", {})
    result = []
    resolved = set()
    for edition, settings in inheritance.items():
        base = settings["base_edition"]
        local_versions = LOCAL_HARD_SURFACE_VERSIONS.get(edition) if product.get("id") == "hard-surface" else None
        if (edition not in editions or base not in editions
                or (base in inheritance and not (local_versions and base in resolved))):
            raise ValueError("Invalid action edition inheritance")
        baseline = {row["action_id"]: row for row in [*rows, *result] if row["version"] == base}
        explicit = {row["action_id"] for row in rows if row["version"] == edition}
        patches = settings.get("action_updates", [])
        updates = {action: patch["fields"] for patch in patches for action in patch["actions"]}
        if len(updates) != sum(len(patch["actions"]) for patch in patches):
            raise ValueError("Duplicate inherited action update")
        if not baseline or updates.keys() - (baseline.keys() - explicit):
            raise ValueError("Inherited action updates no longer match the versioned index")
        for action, old in baseline.items():
            if action in explicit:
                continue
            fields = updates.get(action, {})
            if fields.keys() - (old.keys() - {"product", "version", "action_id"}):
                raise ValueError("Invalid inherited action fields")
            row = {**old, "version": edition}
            if old["status"] == "已实跑":
                row["status"] = "有对应待实跑"
                row["gap_owner"] = product["action_group"] + " / 公共 CLI"
            historical = old["evidence_file"] or old["evidence"]
            row.update(evidence=f"基版 {base} 的历史参考见 {historical}",
                       evidence_file="", reference_type="旧版证据")
            if local_versions:
                # Some base examples contain an old release or a historical host as literal text.
                previous, current = local_versions
                row["cli_parameter"] = (row["cli_parameter"].removeprefix("5.3.0，公开 0.3.2；")
                                        .removeprefix("5.3.0；").replace(f"edition={previous}", f"edition={current}"))
                row["input"] = row["input"].replace(f"{previous} 面板", f"{current} 面板")
                row["gui_source"] = ""
            row.update(fields)
            result.append(row)
        resolved.add(edition)
    return result


def read_action_index(data):
    groups = defaultdict(list)
    seen = set()
    for index in [data["action_index"], *data.get("action_supplements", [])]:
        source = CATALOG / index["file"]
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != index["sha256"]:
            raise ValueError(f"Action index fingerprint differs from the catalog: {source.name}")
        with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                identity = row["product"], row["version"], row["action_id"]
                if identity in seen:
                    raise ValueError(f"Duplicate versioned action in {source.name}: {identity}")
                seen.add(identity)
                groups[row["product"]].append(row)
    return groups


def apply_edition_evidence(product, rows):
    for edition, descriptor in product.get("edition_evidence", {}).items():
        name = descriptor["file"]
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Edition evidence must name a local catalog JSON file")
        raw = (CATALOG / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise ValueError(f"Edition evidence fingerprint differs from the catalog: {name}")
        receipt = json.loads(raw)
        if receipt["product"] != product["id"] or receipt["edition"] != edition:
            raise ValueError(f"Edition evidence identity differs: {name}")
        targets = {row["action_id"]: row for row in rows if row["version"] == edition}
        evidence = {row["action_id"]: row for row in receipt["actions"]}
        if targets.keys() != evidence.keys() or len(evidence) != len(receipt["actions"]):
            raise ValueError(f"Edition evidence must cover each action exactly once: {name}")
        for action, result in evidence.items():
            row = targets[action]
            row["historical_evidence"] = {
                "text": row["evidence"], "file": row["evidence_file"], "type": row["reference_type"],
            }
            row.update(status="已实跑", gap_owner="",
                       evidence=f"Blender {receipt['blender']} build {receipt['build']} 实跑，见动作证据和原始回执。",
                       evidence_file=f"docs/cli/workflows/{name}", reference_type=result["scope"],
                       verification={"blender": receipt["blender"], "build": receipt["build"], **result})


def matching_rows(index, product, args):
    baseline_name = product.get("action_group")
    overrides = product.get("action_updates", {})
    rows = []
    unique_matches = set()
    for original in index.get(baseline_name, []):
        row = original.copy()
        for update in overrides.get(row["action_id"], []):
            if update["version"] == row["version"]:
                unique_matches.add((row["version"], row["action_id"]))
                row.update(update["fields"])
        rows.append(row)
    expected = {(v["version"], k) for k, vv in overrides.items() for v in vv}
    if expected != unique_matches:
        raise ValueError("Action updates no longer match the versioned index")
    keys = {(row["version"], row["action_id"]) for row in rows}
    for row in product.get("additional_actions", []):
        key = (row["version"], row["action_id"])
        if key in keys or row["product"] != baseline_name or row["version"] not in product["editions"]:
            raise ValueError("Invalid additional edition action")
        rows.append(row)
        keys.add(key)
    rows.extend(inherited_actions(product, rows))
    apply_edition_evidence(product, rows)
    results = []
    for row in rows:
        if args.edition and row["version"] != args.edition:
            continue
        if args.action and row["action_id"] != args.action:
            continue
        if args.status and row["status"] != args.status:
            continue
        if args.query and args.query.casefold() not in " ".join(
            row[k] for k in ("label", "action_id", "cli_command", "cli_parameter")
        ).casefold():
            continue
        results.append(row)
    return results


def lookup_rows(data, products, args, evidence_host, blender):
    index = read_action_index(data)
    results = []
    for product in products:
        rows = matching_rows(index, product, args)
        if not args.product:
            rows = [dict(row, product_id=product["id"]) for row in rows]
        results.extend(rows)
    if args.action and not results:
        raise LookupError(f"No matching action for {args.product or 'selected products'}: {args.action}")
    page = results[args.offset:args.offset + args.limit]
    for row in page:
        alternate = row.pop("alternate_edition", "")
        alternate_host = row.pop("alternate_host", "")
        exact_alternates = row.pop("alternate_action_ids", "").split()
        alternate_scope = row.pop("alternate_scope", "")
        if alternate and alternate_host == evidence_host:
            prefix = [str(ROOT / "blenderctl.ps1"), "--blender-version", alternate_host, "discover",
                      "--product", row.get("product_id", args.product), "--edition", alternate]
            row["alternate_edition_lookup"] = {
                "edition": alternate, "blender_version": alternate_host,
                "argv": prefix + (["--action", exact_alternates[0]] if exact_alternates else ["--query", row["action_id"]]),
            }
            if exact_alternates:
                row["alternate_edition_lookup"]["exact_action_argv"] = {
                    action: prefix + ["--action", action] for action in exact_alternates}
            if alternate_scope:
                row["alternate_edition_lookup"]["scope"] = alternate_scope
    if args.action and page:
        from action_routes import attach_launchers

        external_routes = read_json(CATALOG / "action-routes-external.json")["products"]
        product_by_id = {product["id"]: product for product in products}
        for row in page:
            product_id = row.get("product_id", args.product)
            attach_launchers(row, product_id, evidence_host, blender, ROOT, external_routes)
            if product_id == "hard-surface" and row["version"] in LOCAL_HARD_SURFACE_VERSIONS:
                tool = row["action_id"].removeprefix("hard_surface.execute/")
                if tool in LOCAL_HARD_SURFACE_TOOLS:
                    for launcher in row["launchers"]:
                        if launcher["runner"] == "public":
                            launcher["argv"] = [str(ROOT / "blenderctl.ps1"), "--blender", blender,
                                                "model", "flow-tools", "--tool", tool]
            alternative = row.get("alternate_edition_lookup")
            if alternative:
                version = alternative["blender_version"]
                releases = Path(os.environ.get("BLENDERCTL_BLENDER_ROOT") or ROOT / "runtime/blender/releases")
                binary = releases / version / f"blender-{version}-windows-x64" / "blender.exe"
                filters = argparse.Namespace(edition=alternative["edition"], query=None, action=None, status=None)
                candidates = matching_rows(index, product_by_id[product_id], filters)
                options = []
                exact_alternates = alternative.get("exact_action_argv", {})
                for item in candidates:
                    action = item["action_id"]
                    if exact_alternates:
                        if action not in exact_alternates:
                            continue
                    elif action != row["action_id"] and not action.startswith(row["action_id"] + "."):
                        continue
                    if item["status"] not in {"已实跑", "部分对应"}:
                        continue
                    attach_launchers(item, product_id, version, str(binary), ROOT, external_routes)
                    launchers = [launcher for launcher in item["launchers"] if launcher["file_present"]]
                    if launchers and binary.is_file():
                        options.append({"action_id": action, "status": item["status"], "cli_parameter": item["cli_parameter"],
                                        "evidence_file": item["evidence_file"], "launchers": launchers})
                alternative["actions"] = options
    response = {
        "product": args.product, "edition_filter": args.edition,
        "edition_scope": "exact edition" if args.edition else "all audited editions",
        "matched": len(results), "offset": args.offset, "limit": args.limit,
        "next_offset": args.offset + args.limit if args.offset + args.limit < len(results) else None,
        "items": page,
    }
    if args.product:
        product, = products
        group = product.get("action_group")
        response.update(action_group=group, editions=product.get("editions", []),
                        index_scope="专项动作索引" if group else "辅助能力，使用产品列出的技能或现有 CLI 入口",
                        selected_host_evidence=product["blender_evidence"].get(evidence_host))
    else:
        response["index_scope"] = "按所选作品目标查询全部产品，动作实跑状态按发行版统计。"
    if not args.product or args.query:
        page_ids = {row.get("product_id", args.product) for row in page}
        term = args.query if not (args.action or args.edition or args.status) else None
        response["routes"] = lookup_routes(products, page_ids, term, blender, evidence_host)
    return response


def lookup_routes(products, page_ids, query, blender, evidence_host):
    """Include navigation for this page and literal matches in product or launcher descriptions."""
    routes = []
    term = query.casefold() if query else None
    for product in products:
        entries = []
        matches_product = False
        if term:
            terms = [product[field] for field in ("id", "name", "use_when", "skill")]
            terms.extend(product.get("related_skills", []))
            terms.append(product.get("installed_skill", ""))
            matches_product = any(term in value.casefold() for value in terms)
            for entry in entry_points(product, blender, evidence_host):
                if term in " ".join([*entry["arguments"], entry["context"]]).casefold():
                    entries.append(entry)
        if product["id"] in page_ids or entries or matches_product:
            routes.append({
                "product_id": product["id"], "name": product["name"], "skill": product["skill"],
                **skill_locations(product),
                "selected_host_evidence": product["blender_evidence"].get(evidence_host),
                "product_argv": [str(ROOT / "blenderctl.ps1"), "--blender", blender, "discover", "--product", product["id"]],
                "matching_entry_points": entries,
            })
    return routes


def action_summary(data, products, args, evidence_host, blender):
    from action_routes import attach_launchers

    index = read_action_index(data)
    external = read_json(CATALOG / "action-routes-external.json")["products"]
    total = Counter()
    route_counts = Counter()
    items = []
    for product in products:
        editions = {}
        product_routes = Counter()
        for edition in product.get("editions", []):
            if not args.edition or args.edition == edition:
                editions[edition] = Counter()
        for row in matching_rows(index, product, args):
            editions[row["version"]][row["status"]] += 1
            attach_launchers(row, product["id"], evidence_host, blender, ROOT, external)
            key = "with_launcher" if any(item["file_present"] for item in row["launchers"]) else "without_launcher"
            product_routes[key] += 1
        route_counts.update(product_routes)
        for counts in editions.values():
            total.update(counts)
        items.append({
            "product": product["id"], "name": product["name"], "skill": product["skill"],
            "selected_host_evidence": product["blender_evidence"].get(evidence_host),
            "selected_host_routes": dict(product_routes),
            "editions": [{"edition": edition, "matched": counts.total(), "status_counts": counts}
                         for edition, counts in editions.items()],
        })
    return {"scope": "历史目录按产品版本、动作和参数分别计数。已实跑包含旧宿主证据。5.3.0 验收单独核对具体动作及产物。",
            "selected_host_routes": {"host": evidence_host, **route_counts,
                                     "scope": "按当前宿主解析且入口文件存在的分版记录。实际作品运行见对应回执。"},
            "matched": total.total(), "status_counts": total, "products": items}


def entry_points(product, blender, evidence_host=None):
    from action_routes import script_prefix

    resolved = []
    for entry in product.get("entry_points", []):
        if "hosts" in entry and evidence_host not in entry["hosts"]:
            continue
        path = ROOT / entry["path"]
        if entry["type"] == "public":
            command = [str(path), "--blender", blender, *entry["arguments"]]
            available = path.is_file()
        else:
            prefix, available = script_prefix(entry["type"], path, entry, blender)
            command = [*prefix, *entry["arguments"]]
        resolved.append({**entry, "argv": command, "file_present": available})
    from production_contract import PACKAGES, package_root, selected_release
    domain = product.get("id")
    if domain in PACKAGES and package_root(domain).is_dir():
        path = ROOT / "blenderctl.ps1"
        release = selected_release()
        resolved.append({
            "type": "public", "path": "blenderctl.ps1", "arguments": ["request", "--help"],
            "context": "用 request 提交 Blender 5.3 production.run，独立工程可在 pipeline 中并行。",
            "release": release,
            "host_evidence": evidence_host,
            "argv": [str(path), "--blender", blender, "request", "--help"],
            "file_present": path.is_file(),
            "request_schema": {"schema_version": "1.0", "command": "production.run",
                               "params": {"domain": domain, "release": release, "file": "<绝对 .blend 路径>",
                                          "expected_sha256": "<小写 SHA-256>", "request": {}}},
            "guide": str(ROOT / "docs/cli/安装与快速开始.md"),
        })
    return resolved


def local_skill_file(name):
    for base in (ROOT / ".agents/skills", ROOT / "skills", Path.home() / ".agents/skills",
                 Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills"):
        file = base / name / "SKILL.md"
        if file.is_file():
            return str(file)
    return None


def plugin_skill_file(qualified):
    """Resolve a package file only when its local version is unambiguous."""
    plugin, _, skill = qualified.partition(":")
    if not plugin or not skill:
        return None
    cache = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "plugins/cache"
    manifests = list(cache.glob(f"*/{plugin}/*/.codex-plugin/plugin.json"))
    if len(manifests) != 1:
        return None
    manifest, = manifests
    package = manifest.parent.parent
    try:
        data = read_json(manifest)
        if not isinstance(data, dict) or data.get("name") != plugin or data.get("version") != package.name:
            return None
        if not isinstance(data.get("skills"), str):
            return None
        path = (package / data["skills"] / skill / "SKILL.md").resolve()
        if path.is_relative_to(package.resolve()) and path.is_file():
            return str(path)
    except (OSError, ValueError):
        pass
    return None


def skill_locations(product):
    local = local_skill_file(product["skill"])
    result = {}
    if "installed_skill" in product:
        result["installed_skill"] = product["installed_skill"]
        local = local or plugin_skill_file(product["installed_skill"])
    result["local_skill_file"] = local
    if "related_skills" in product:
        result["related_skill_files"] = {name: local_skill_file(name) for name in product["related_skills"]}
    return result


def discover(args):
    binary = resolve_blender(args.blender)
    version = args.host_version.split(".")
    if len(version) != 3 or not is_supported_host(args.host_version):
        raise ValueError(f"Discovery requires Windows Blender 5.3.0 or later. Selected version: {args.host_version}")
    if args.windows_product and args.windows_product.casefold() != "blender":
        raise ValueError(f"Selected executable reports a different Windows product ({args.windows_product}): {binary}")
    evidence_host = catalog_host(binary, args.host_version)
    selected_blender = {"version": args.host_version, "file": str(binary)}
    if evidence_host is None:
        selected_blender["audited"] = False
    data = read_json(CATALOG / "products.json")
    from plugin_registry import load_registry
    registry = load_registry(ROOT)
    registered = {p["domain"]: p for p in registry["plugins"]}
    products = [*data["products"], *registry.get("standalone_products", [])]
    selected = [p for p in products if not args.product or p["id"] == args.product]
    if not selected:
        raise LookupError("Unknown product: " + args.product)
    if args.goal:
        if args.goal not in data["goals"]:
            raise LookupError("Unknown goal: " + args.goal)
        selected = [p for p in selected if args.goal in p["goals"]]
        if not selected and args.product:
            raise LookupError(f"{args.product} is not listed for {args.goal}")
    result = {
        "catalog_version": data["version"],
        "registered_plugin_count": len(registered),
        "workspace": str(ROOT),
        "selected_blender": selected_blender,
        "goals": data["goals"],
        "build_policy": data["build_policy"],
    }
    if args.product == "hard-surface" and args.edition == "0.3.7":
        args.edition = HARD_SURFACE_LOCAL_EDITION
    if args.edition and args.edition not in selected[0].get("editions", []):
        raise LookupError(f"Unknown edition for {selected[0]['id']}: {args.edition}")
    if args.summary:
        from production_contract import selected_release
        result["cli_version"] = VERSION
        result["production_release"] = selected_release()
        result["summary"] = action_summary(data, selected, args, evidence_host, str(binary))
        return result
    if args.actions or args.action or args.query:
        result["actions"] = lookup_rows(data, selected, args, evidence_host, str(binary))
        return result
    result["products"] = []
    for p in selected:
        internal = {"entry_points", "action_updates", "additional_actions", "edition_inheritance", "edition_evidence"}
        item = {k: v for k, v in p.items() if k not in internal}
        if p["id"] in registered:
            item["registered_plugin"] = registered[p["id"]]
        item.update(skill_locations(p))
        item["entry_points"] = entry_points(p, str(binary), evidence_host)
        result["products"].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(prog="blenderctl discover",
                                     description="Find project tools, supported production contexts and audited actions.")
    parser.add_argument("--blender", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--host-version", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--windows-product", default="", help=argparse.SUPPRESS)
    parser.add_argument("--goal", help="production goal ID from the list")
    parser.add_argument("--product", help="product ID from the list")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--actions", action="store_true", help="list audited actions across products or for one product")
    mode.add_argument("--action", help="exact action ID, including branch suffix; returns edition-specific launchers, "
                                      "read-only help argv and a command_argv prefix for the action inputs")
    mode.add_argument("--summary", action="store_true", help="count actions by product, edition and evidence status")
    parser.add_argument("--edition", help="exact product build as listed in editions")
    parser.add_argument("--status", choices=("已实跑", "有对应待实跑", "部分对应", "缺CLI", "待核", "仅界面", "该版停用"),
                        help="Filter by action status. 该版停用 means the plug-in itself disables the action in this edition.")
    parser.add_argument("--query", help="find action names, labels, commands or parameters; also locate matching product and launcher descriptions")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.edition and not args.product:
        parser.error("--edition requires --product")
    if (args.status or args.edition) and not (args.actions or args.action or args.query or args.summary):
        parser.error("action filters require --query, --actions, --action or --summary")
    if (args.offset or args.limit != 25) and (args.summary or not (args.actions or args.action or args.query)):
        parser.error("pagination requires --query, --actions or --action")
    if args.offset < 0 or not 1 <= args.limit <= 250:
        parser.error("offset must be non-negative and limit between 1 and 250")
    try:
        body = {"ok": True, "command": "discover", "data": discover(args)}
        code = 0
    except (Failure, LookupError, ValueError, OSError, EOFError) as error:
        body = {"ok": False, "command": "discover", "error": str(error)}
        code = 2
    print(json.dumps(body, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
