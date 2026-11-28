"""Shell access to the existing owned relight session and its verified live preview."""
from pathlib import Path
from contextlib import nullcontext
import re
import time


def add_parser(actions):
    parser = actions.add_parser("depth-control", help="edit an existing GPU relight session and read the corresponding live image")
    operations = parser.add_subparsers(dest="control", required=True)
    for name, help_text in (
        ("inspect", "read the latest submitted recipe and revision"),
        ("submit", "submit complete revision parameters against a known current revision"),
        ("read", "read the exact live preview using the revision and fingerprint from submit"),
        ("stop", "request a graceful close, then use job wait for the terminal result"),
    ):
        action = operations.add_parser(name, help=help_text)
        action.add_argument("job_id", metavar="JOB_ID")
        if name == "submit":
            action.add_argument("--after-revision", type=int, required=True)
            action.add_argument("--params", type=Path, required=True, help="UTF-8 JSON with config, kind and optional local_edits")
            action.add_argument("--reason", default="cli-edit")
        if name == "read":
            action.add_argument("--revision", type=int, required=True)
            action.add_argument("--parameters-sha256", required=True)
            action.add_argument("--wait-seconds", type=int, choices=range(56), metavar="0..55", default=0)


def control(job, args):
    from filesystem import PathLocks

    # A CLI read can proceed while another caller edits. Serialize CLI writers from
    # revision discovery to the native no-replace publish so retries cannot fork history.
    lock = PathLocks([job / "session/inbox"]) if args.control in {"stop", "submit"} else nullcontext()
    with lock:
        return _control(job, args)


def _control(job, args):
    from compositing_relight_session_client import SessionClient, read, sha
    from compositing_relight_session_contract import DepthError, check_message, normalized_parameters
    from protocol import CODES, Failure

    ready_path = job / "session-ready.json"
    if not ready_path.is_file():
        raise Failure("NOT_FOUND", "Session is not ready. Inspect this job's status before retrying.")
    if ready_path.resolve().parent != job:
        raise Failure("CONFLICT", "Session ready file belongs to another directory")
    try:
        client = SessionClient(ready_path)
        ready = client.ready
        if args.control == "inspect":
            recipe = {"config": ready["initial_config"], "kind": "preview"}
            fingerprint = None
            if client.revision:
                message = read(client.inbox / f"{client.revision:09}.json")
                recipe = check_message(message, ready, ready["initial_config"])
                if message["revision"] != client.revision:
                    raise Failure("CONFLICT", "Revision filename and message disagree")
                fingerprint = message["parameters_sha256"]
            return {"session_id": ready["session_id"], "revision": client.revision, "parameters_sha256": fingerprint,
                    "params": recipe, "close_requested": (job / "session/stop.json").is_file()}
        if args.control == "stop":
            client.stop()
            return {"state": "close_requested"}
        if args.control == "submit":
            if args.after_revision != client.revision:
                raise Failure("CONFLICT", f"Current revision is {client.revision}. Inspect the current recipe before editing.")
            if (job / "session/stop.json").exists():
                raise Failure("CONFLICT", "Session close was requested")
            if client.revision >= read(job / "request.json")["params"]["max_revisions"]:
                raise Failure("RESOURCE_LIMIT", "Session revision budget reached")
            params = normalized_parameters(read(args.params), ready["initial_config"])
            sent = client.submit(**params, reason=args.reason)
            return {"state": "submitted", "revision": sent["revision"],
                    "parameters_sha256": sent["parameters_sha256"], "file": sent["file"]}
        if not re.fullmatch("[0-9a-f]{64}", args.parameters_sha256):
            raise Failure("INVALID_REQUEST", "Expected a lowercase SHA-256 from submit")
        if not 1 <= args.revision <= client.revision:
            raise Failure("NOT_FOUND", "Unknown submitted revision")
        message = read(client.inbox / f"{args.revision:09}.json")
        check_message(message, ready, ready["initial_config"])
        if message["revision"] != args.revision or message["parameters_sha256"] != args.parameters_sha256:
            raise Failure("CONFLICT", "Revision or parameter fingerprint differs")
        # The pinned GUI client tracks its last submission in memory. Restore that
        # cursor from its own validated inbox so a new shell can reuse the same poll.
        client._desired = message
        deadline = time.monotonic() + args.wait_seconds
        result = {"revision": args.revision, "parameters_sha256": args.parameters_sha256}
        while True:
            published = client.poll()
            if published is not None:
                result.update(state="published", display=published["display"], output=published["output"])
                if "linear_output" in published:
                    result["linear_output"] = published["linear_output"]
                folder = Path(published["display"]["file"]).parent
                result["publication_receipt"] = str(folder / "host-verified.json")
                for member in published["members"]:
                    if member["role"] == "diagnostic_srgb_png":
                        file = folder / "view.png"
                        if Path(member["file"]).resolve() != file or file.stat().st_size != member["bytes"] or sha(file) != member["sha256"]:
                            raise Failure("CONFLICT", "Diagnostic image differs from its publication receipt")
                        result["diagnostic"] = member
                return result
            if client.last_reason == "stale_or_alien":
                latest = read(job / "session/latest.json")["request"]
                if latest.get("session_id") == ready["session_id"] and latest["revision"] > args.revision:
                    return dict(result, state="superseded", latest_revision=latest["revision"])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(result, state="pending")
            time.sleep(min(remaining, 0.1))
    except FileExistsError as error:
        raise Failure("CONFLICT", "Another caller advanced this session. Inspect the latest revision.") from error
    except DepthError as error:
        raise Failure(error.code if error.code in CODES else "INVALID_REQUEST", str(error)) from error
    except RuntimeError as error:
        raise Failure("CONFLICT", str(error)) from error
