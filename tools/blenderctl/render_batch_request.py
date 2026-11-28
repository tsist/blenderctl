"""Translate structured render batch requests into the published batch CLI arguments."""

import json
import re
import sys

from protocol import CODES, SCHEMA, Failure, envelope, read_json


COMMAND_PREFIX = "render.batch."
SUBMIT_FIELDS = ("manifest", "ffmpeg", "ffprobe")
CONTROL_ACTIONS = {"status", "wait", "cancel", "resume"}


def batch_arguments(document):
    if (not isinstance(document, dict) or set(document) != {"schema_version", "command", "params"}
            or document["schema_version"] != SCHEMA or not isinstance(document["params"], dict)):
        raise Failure("INVALID_REQUEST", f"Expected schema_version {SCHEMA}, command and object params")
    command = document["command"]
    if not isinstance(command, str) or not command.startswith(COMMAND_PREFIX):
        raise Failure("INVALID_REQUEST", "Expected a render.batch command")
    action = command.removeprefix(COMMAND_PREFIX)
    params = document["params"]
    argv = ["render", "batch", action]
    if action == "submit":
        if set(params) != set(SUBMIT_FIELDS) or any(
            not isinstance(params[key], str) or not params[key].strip() or "\0" in params[key]
            for key in SUBMIT_FIELDS
        ):
            raise Failure("INVALID_REQUEST", "Submit requires manifest, ffmpeg and ffprobe file paths")
        argv.extend(f"--{key}={params[key]}" for key in SUBMIT_FIELDS)
    elif action in CONTROL_ACTIONS:
        allowed = {"batch_id", "wait_seconds"} if action == "wait" else {"batch_id"}
        batch_id = params.get("batch_id")
        if not set(params) <= allowed or not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{16}", batch_id):
            raise Failure("INVALID_REQUEST", "Expected a 16-character lowercase hexadecimal batch_id and valid action fields")
        argv.append(batch_id)
        if "wait_seconds" in params:
            seconds = params["wait_seconds"]
            if type(seconds) is not int or not 0 <= seconds <= 55:
                raise Failure("INVALID_REQUEST", "wait_seconds must be an integer from 0 to 55")
            argv.append(f"--wait-seconds={seconds}")
    else:
        raise Failure("INVALID_REQUEST", f"Unknown command: {command}")
    return argv


def main(arguments):
    command = None
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if len(arguments) != 1:
            raise Failure("INVALID_REQUEST", "Provide exactly one render batch request file")
        document = read_json(arguments[0])
        if isinstance(document, dict) and isinstance(document.get("command"), str):
            command = document["command"]
        print(json.dumps(batch_arguments(document), ensure_ascii=False, allow_nan=False))
    except (Failure, OSError) as exc:
        code = exc.code if isinstance(exc, Failure) else "IO_ERROR"
        print(json.dumps(envelope(command, error={"code": code, "message": str(exc)}),
                         ensure_ascii=False, allow_nan=False))
        return CODES[code]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
