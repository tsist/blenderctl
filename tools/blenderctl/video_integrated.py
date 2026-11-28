"""Public arguments for the registry-selected Video Workflow implementation."""

from pathlib import Path

from protocol import Failure, SCHEMA, read_json


def add_arguments(sub):
    video = sub.add_parser(
        "video", help="protected saved-file VSE editing",
        epilog="Use video job {status,result,wait,cancel} for saved job receipts. "
               "Existing video ripple and video otio commands use their compatible release.",
    )
    commands = video.add_subparsers(dest="action", required=True, parser_class=type(video))
    describe = commands.add_parser("describe", help="show the versioned native video contract")
    describe.add_argument("--operation", choices=("run", "inspect", "manifest", "output", "clip"))
    describe.add_argument("--schema", action="store_true")
    describe.add_argument("--section")
    for name in ("run", "inspect"):
        action = commands.add_parser(name)
        action.add_argument("file")
        action.add_argument("--expected-sha256", required=True)
        action.add_argument("--resources", type=Path, required=True)
        if name == "run":
            action.add_argument("--manifest", type=Path, required=True)
            action.add_argument("--output", type=Path)
            action.add_argument("--resume", type=Path)
            action.add_argument("--resume-sha256")
        else:
            action.add_argument("--scene", required=True)
            action.add_argument("--workflow-id")


def request_from_args(args):
    params = {
        "file": str(Path(args.file).resolve()), "expected_sha256": args.expected_sha256,
        "resources": read_json(args.resources),
    }
    if args.action == "run":
        params["manifest"] = read_json(args.manifest)
        if args.output:
            params["output"] = read_json(args.output)
        if bool(args.resume) != bool(args.resume_sha256):
            raise Failure("INVALID_REQUEST", "--resume and --resume-sha256 must be provided together")
        if args.resume:
            params["resume"] = {"file": str(args.resume.resolve()), "expected_sha256": args.resume_sha256}
    else:
        params["scene"] = args.scene
        if args.workflow_id is not None:
            params["workflow_id"] = args.workflow_id
    return {"schema_version": SCHEMA, "command": f"video.{args.action}", "params": params}
