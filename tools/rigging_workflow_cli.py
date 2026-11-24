# SPDX-License-Identifier: GPL-3.0-or-later
"""Dispatch to the separately maintained Rigging Workflow v003 CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().with_name("blenderctl")))
from cli import Parser, portable_blender
from protocol import CODES, Failure, envelope


EDITION = "0.42.3-agent-v003"
ROOT_VARIABLE = "BLENDERCTL_RIGGING_ROOT"
# These are only eligible for Linux; a compatible backend must opt in per mode.
LINUX_READ_MODES = {
    "candidate-entry": {"inspect"},
    "candidate-selected-skin": {"inspect"},
    "candidate-repair": {"inspect"},
    "candidate-skinning-policy": {"qualify", "plan"},
}

ROUTES = {
    "candidate-entry": (
        "character_entry_candidate.py", ("inspect", "scope", "result", "audition", "finish"),
        "inspect and bind a new character",
    ),
    "candidate-selected-skin": (
        "selected_skinning_cli.py", ("inspect", "execute"),
        "inspect or recalculate weights for selected bones on explicit meshes",
    ),
    "candidate-repair": (
        "rigging_candidate.py", ("inspect", "review-handoff", "apply", "cancel"),
        "inspect, review or repair an existing rig",
    ),
    "candidate-skinning-policy": (
        "skinning_policy_candidate.py", ("qualify", "plan", "preview", "apply"),
        "inspect or apply a bounded donor weight policy",
    ),
}


def parser():
    root = Parser(description="Call the standalone Rigging Workflow v003 commands.")
    runtime = root.add_mutually_exclusive_group()
    runtime.add_argument("--blender", type=Path, help="Blender 5.3.0 executable (required as an absolute path on Linux)")
    runtime.add_argument("--blender-version", choices=("5.3.0",), help="select a Windows portable installation from BLENDERCTL_BLENDER_ROOT")
    subparsers = root.add_subparsers(dest="command", required=True)
    group = subparsers.add_parser("rigging-workflow")
    group.add_argument("--rigging-root", type=Path, help=f"checkout root of blender-rigging-workflow (or {ROOT_VARIABLE})")
    actions = group.add_subparsers(dest="action", required=True)
    actions.add_parser("discover", help="list commands, supported modes and source availability")
    for name, (_, modes, description) in ROUTES.items():
        route = actions.add_parser(name, help=description, description=description)
        route.add_argument("mode", choices=modes)
        route.add_argument("--request", type=Path, required=name != "candidate-repair", help="absolute request JSON path")
        route.add_argument("--run-dir", type=Path, required=True, help="absolute run directory")
    return root


def _runtime_root(option):
    root = option
    if root is None and os.environ.get(ROOT_VARIABLE):
        root = Path(os.environ[ROOT_VARIABLE])
    if root is None:
        return None
    if not root.is_absolute():
        raise Failure("INVALID_REQUEST", f"--rigging-root and {ROOT_VARIABLE} require an absolute checkout path")
    return (root / "cli-v003").resolve()


def _script(root, name):
    if root is None or not (root / "rigging_workflow" / "__init__.py").is_file():
        return None
    path = root / "cli" / ROUTES[name][0]
    return path if path.is_file() else None


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate JSON key")
    return fields


def _linux_modes(root):
    if root is None:
        return {}
    manifest = root / "linux-capabilities.json"
    if not manifest.exists() and not manifest.is_symlink():
        return {}
    try:
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("expected a local regular file")
        with manifest.open("rb") as source:
            data = source.read(4097)
        if len(data) > 4096:
            raise ValueError("manifest exceeds size limit")
        document = json.loads(data, object_pairs_hook=_unique_fields)
        if not isinstance(document, dict) or set(document) != {"schema_version", "edition", "routes"}:
            raise ValueError("invalid manifest fields")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["edition"] != EDITION:
            raise ValueError("incompatible schema or edition")
        routes = document["routes"]
        if not isinstance(routes, dict) or not set(routes) <= set(LINUX_READ_MODES):
            raise ValueError("invalid routes")
        for name, modes in routes.items():
            if (not isinstance(modes, list) or not all(isinstance(mode, str) for mode in modes)
                    or len(set(modes)) != len(modes) or not set(modes) <= LINUX_READ_MODES[name]
                    or _script(root, name) is None):
                raise ValueError(f"invalid Linux read-mode declaration for {name}")
        return routes
    except (OSError, ValueError, RecursionError) as error:
        raise Failure("INVALID_REQUEST", f"Invalid Rigging Workflow Linux capability manifest: {error}") from error


def _blender(args):
    if sys.platform == "win32":
        return (args.blender or portable_blender(args.blender_version or "5.3.0")).resolve()
    if sys.platform != "linux":
        raise Failure("UNSUPPORTED", "The rigging adapter is available on Windows and Linux")
    path = args.blender
    if path is None or not path.is_absolute():
        raise Failure("INVALID_REQUEST", "On Linux supply --blender with the absolute Blender executable path")
    if not path.is_file() or path.resolve() != path or not os.access(path, os.X_OK):
        raise Failure("INVALID_REQUEST", "On Linux --blender must name a canonical executable file, not a symlink")
    return path


def dispatch(args):
    root = _runtime_root(args.rigging_root)
    linux = _linux_modes(root) if sys.platform == "linux" else {}
    if args.action == "discover":
        print(json.dumps({
            "ok": True,
            "edition": EDITION,
            "source_configured": root is not None,
            "host": sys.platform,
            "source_availability_only": True,
            "commands": [
                {"command": f"rigging-workflow {name}", "modes": modes, "available": _script(root, name) is not None,
                 **({"linux_modes": linux.get(name, [])} if sys.platform == "linux" else {})}
                for name, (_, modes, _) in ROUTES.items()
            ],
        }))
        return 0

    script = _script(root, args.action)
    if script is None:
        raise Failure("NOT_FOUND", f"Set {ROOT_VARIABLE} or --rigging-root to the blender-rigging-workflow checkout containing cli-v003")
    if sys.platform == "linux" and args.mode not in linux.get(args.action, []):
        raise Failure("UNSUPPORTED", "This mode is not qualified by the selected Rigging Workflow backend on Linux")
    command = [sys.executable, str(script), args.mode, "--run-dir", str(args.run_dir)]
    if args.mode == "cancel":
        if args.request is not None:
            raise Failure("INVALID_REQUEST", "Repair cancellation takes only the existing --run-dir")
    else:
        if args.request is None:
            raise Failure("INVALID_REQUEST", "--request is required for this mode")
        command.extend(("--request", str(args.request), "--blender", str(_blender(args))))
    return subprocess.run(command, check=False).returncode


def main():
    try:
        return dispatch(parser().parse_args())
    except Failure as error:
        code = error.code
        message = str(error)
    except OSError as error:
        code = "IO_ERROR"
        message = str(error)
    print(json.dumps(envelope(error={"code": code, "message": message}), ensure_ascii=False))
    return CODES[code]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
