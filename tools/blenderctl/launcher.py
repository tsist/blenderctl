"""Dispatch the public project CLI after the PowerShell Python bootstrap."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from host_compat import is_supported_host
from protocol import CODES, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, Failure, ROOT, envelope, read_json


CORE = Path(__file__).resolve().parent
VALUE_FLAGS = {"--blender", "--blender-version", "--edition", "--jobs-dir", "--transactions-dir", "--timeout"}


def split_arguments(argv):
    values = {}
    options = []
    index = 0
    while index < len(argv) and argv[index].startswith("--") and argv[index] != "--":
        flag, inline, value = argv[index].partition("=")
        index += 1
        if flag not in VALUE_FLAGS:
            options.append(argv[index - 1])
            continue
        if not inline:
            if index == len(argv) or argv[index].startswith("--"):
                raise Failure("INVALID_REQUEST", f"{flag} requires a value")
            value = argv[index]
            index += 1
        if flag in values:
            raise Failure("INVALID_REQUEST", f"Specify {flag} once")
        values[flag] = value
        if flag not in {"--blender", "--blender-version", "--edition"}:
            options.append(f"{flag}={value}")
    return options, values, argv[index:]


def invoke(entry, args, *, compact=False, env=None, fields=None):
    path = ROOT / entry
    if not path.is_file():
        raise Failure("NOT_FOUND", f"Missing CLI entry: {path}")
    command = [sys.executable, "-B", str(path), *args]
    if not compact and not fields:
        return subprocess.call(command, env=env)
    process = subprocess.run(command, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    try:
        payload = json.loads(process.stdout)
        if fields and isinstance(payload, dict) and payload.get("domain") == "toon-workflow":
            payload.update(fields)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":") if compact else None))
    except ValueError:
        print(process.stdout, end="")
    return process.returncode


def request_document(tail):
    if len(tail) >= 2 and Path(tail[1]).is_file():
        document = read_json(tail[1])
        if isinstance(document, dict) and isinstance(document.get("command"), str):
            return document
    return {}


def render_batch(options, values, tail, version, edition, compact):
    if "--jobs-dir" not in values:
        jobs = DEFAULT_JOBS.parent / "render-batches"
        options.append(f"--jobs-dir={jobs}")
    else:
        jobs = Path(values["--jobs-dir"])
    if "--transactions-dir" not in values:
        options.append(f"--transactions-dir={DEFAULT_TRANSACTIONS.parent / 'render-batch-transactions'}")
    if "--timeout" not in values:
        options.append("--timeout=7200")
    if not edition and len(tail) >= 4 and tail[2] in {"status", "wait", "cancel", "resume"}:
        if re.fullmatch("[0-9a-f]{16}", tail[3]):
            status = jobs / "b" / tail[3] / "status.json"
            if status.is_file() and read_json(status).get("runtime_release") != "1.0.0":
                edition = "auto"
    if edition and edition != "1.0.0":
        return invoke(CORE / "render_batch_dispatch.py", [version, edition, *options, *tail], compact=compact)
    return invoke(CORE / "cli.py", [*options, *tail], compact=compact)


def launch(binary, version, raw):
    registry = read_json(CORE / "registry.json")["launcher"]
    if not is_supported_host(version):
        raise Failure("UNSUPPORTED", f"Expected Windows Blender 5.3.0 or later. Selected: {version}")
    boundary = raw.index("--") if "--" in raw else len(raw)
    compact = "--compact" in raw[:boundary]
    arguments = [arg for arg in raw[:boundary] if arg != "--compact"] + raw[boundary:]
    options, values, tail = split_arguments(arguments)
    if compact and "--full" in options:
        raise Failure("INVALID_REQUEST", "Choose --compact or --full")
    options.insert(0, f"--blender={binary}")
    domain, action = (tail + ["", ""])[:2]
    document = request_document(tail) if domain == "request" else {}
    command = document.get("command", "")
    routed_domain = command.split(".", 1)[0] if command else domain
    edition = values.get("--edition")
    edition_domain, edition_version = "", ""
    if edition:
        edition_domain, at, edition_version = edition.partition("@")
        if not at or edition_version not in registry.get("editions", {}).get(edition_domain, []):
            raise Failure("INVALID_REQUEST", f"Unknown edition: {edition}")
    help_requested = action in {"--help", "-h"}
    key = f"{domain} {action}"
    handoffs = registry["handoffs"]
    lookups = registry["lookups"]

    if domain == "discover":
        return invoke(CORE / "workflow_discovery.py",
                      [*tail[1:], f"--blender={binary}", f"--host-version={version}", "--windows-product=Blender"],
                      compact=compact)
    if domain == "preview":
        return invoke("tools/viewport_preview/cli.py", [*options, *(["--compact"] if compact else []), *tail[1:]])
    if key in lookups:
        return invoke(lookups[key], [options[0], *[arg for arg in options if arg.startswith("--timeout=")], *tail[2:]],
                      compact=compact)
    if key in handoffs:
        route = handoffs[key]
        if "alias_for" in route:
            route = handoffs[route["alias_for"]]
        entry = route.get("edition_entries", {}).get(edition_version, route["entry"])
        forwarded = tail[1:] if route.get("forward_subcommand", True) else tail[2:]
        if route.get("host_adapter") == "c5_compositor":
            from c5_workflow_host import compose
            return compose(binary, options, forwarded, compact=compact)
        if route.get("host_adapter") == "video_master_audio":
            return invoke(entry, forwarded, compact=compact)
        return invoke(entry, [*options, *forwarded], compact=compact)
    if command.startswith("render.batch."):
        from render_batch_request import batch_arguments
        tail = batch_arguments(document)
    if tail[:2] == ["render", "batch"]:
        return render_batch(options, values, tail, version, edition_version or None, compact)
    if routed_domain == "physics":
        return invoke(CORE / "physics_dispatch.py", [f"--host-version={version}", *options, *tail], compact=compact)
    if routed_domain == "video":
        if edition_domain == "video":
            options.insert(0, f"--legacy-video-edition={edition_version}")
        return invoke(CORE / "video_dispatch.py", [*options, *(["--compact"] if compact else []), *tail])
    if domain == "camera" and help_requested:
        return invoke(CORE / "camera_shot_table.py", [*options, "--help"])
    if routed_domain == "toon-workflow":
        from toon_route import select_toon_bundle
        entry = select_toon_bundle(registry, edition_version if edition_domain == "toon" else None)
        env = {**os.environ, "TOON_WORKFLOW_CLI_CORE_ROOT": str(CORE)}
        fields = {"selected_blender": {"version": version, "file": str(binary)}} if action == "describe" else None
        return invoke(entry, [*options, *(["--compact"] if compact and action != "describe" else []), *tail],
                      compact=compact and action == "describe", env=env, fields=fields)
    core_compact = not help_requested and action not in {"describe", "depth-control", "session", "lease"} and domain not in {
        "capabilities", "resource", "cache"
    }
    result = invoke(CORE / "cli.py", [*options, *(["--compact"] if compact and core_compact else []), *tail],
                    compact=compact and not core_compact)
    if result == 0 and help_requested:
        routes = sorted(name for name in {*handoffs, *lookups} if name.startswith(domain + " "))
        if routes:
            print("Project commands (use --help for each): " + " | ".join(routes))
    if result == 0 and ((not domain and "--help" in options) or domain == "-h"):
        print("Project discovery: discover [--goal GOAL] [--product ID] [--action ID | --summary]")
        print("Project handoffs: preview, physics, toon-workflow, camera and render batch. Use discover to inspect available actions.")
    return result


def main(args):
    binary, version, *raw = args
    try:
        return launch(binary, version, raw)
    except (Failure, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, Failure) else "INVALID_REQUEST"
        print(json.dumps(envelope(error={"code": code, "message": str(exc)}), ensure_ascii=False))
        return CODES[code]
