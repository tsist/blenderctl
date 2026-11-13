"""Command-line interface for Physics Nodes Workflow (isolated CLI overlay v1)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Support direct execution (`python tools/physics_workflow_cli_v1/cli.py ...`) and package execution
_CLI_DIR = Path(__file__).resolve().parent
if str(_CLI_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CLI_DIR.parent))

from physics_workflow_cli_v1 import protocol, runner


def _load_json_arg(spec: str) -> dict:
    p = Path(spec)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(spec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="physics-workflow", description="Blender 5.2.1 LTS Physics Nodes CLI")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("describe", help="Return contract capabilities, presets, domains, and bounds")

    for name in ("run", "sweep", "batch"):
        p = sub.add_parser(name, help=f"Execute physics workflow {name}")
        p.add_argument("--request", required=True, help="Path to JSON request file or inline JSON string")
        p.add_argument("--jobs-dir", required=True, help="Directory for job logs, bakes, and receipts")
        p.add_argument("--timeout-seconds", type=int, default=300, help="Per-Blender-invocation timeout seconds")

    args = parser.parse_args(argv)

    if args.subcommand == "describe":
        resp = runner.describe()
        print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
        return protocol.EXIT_OK

    try:
        payload = _load_json_arg(args.request)
    except Exception as exc:
        err = protocol.CommandResponse(
            ok=False,
            command=args.subcommand,
            job_id=protocol.new_job_id("err"),
            error={"code": "VALIDATION_ERROR", "message": f"Invalid request JSON: {exc}"},
        )
        print(json.dumps(err.to_dict(), indent=2, ensure_ascii=False))
        return protocol.EXIT_VALIDATION

    jobs_dir = Path(args.jobs_dir)
    if args.subcommand == "run":
        resp = runner.run_single(payload, jobs_dir, timeout_seconds=args.timeout_seconds)
    elif args.subcommand == "sweep":
        resp = runner.run_sweep(payload, jobs_dir, timeout_seconds=args.timeout_seconds)
    else:
        resp = runner.run_batch(payload, jobs_dir, timeout_seconds=args.timeout_seconds)

    print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
    if resp.ok:
        return protocol.EXIT_OK
    code = (resp.error or {}).get("code", "")
    if code == "VALIDATION_ERROR":
        return protocol.EXIT_VALIDATION
    if code == "VERIFICATION_FAILED":
        return protocol.EXIT_VERIFICATION
    return protocol.EXIT_EXECUTION


if __name__ == "__main__":
    sys.exit(main())
