"""Route new Video jobs and preserve immutable legacy job readers and resume paths."""

from pathlib import Path
import re
import subprocess
import sys

from protocol import CODES, DEFAULT_JOBS, Failure, envelope, read_json
from video_identity import ROOT, implementation_sha256


HERE = Path(__file__).resolve().parent
LATEST_LEGACY = "0.1.5"
CURRENT_EDITION = "1.0.0"
FROZEN_IDENTITIES = {
    "0.2.0": "fc59da35058548703a531d4331fb03dc2cddcc86cd3a1d81c03cf99df6a5b16b",
    LATEST_LEGACY: "06d63be54ea72ab95090a15fc8949a60c761808f6f16c9eb77e80e55314ed400",
    "0.1.4": "846014645bab95ee82f862dbd72979e2c3260f7b1470bb0781f6fccebe57a8f8",
}
LEGACY_EDITIONS = tuple(FROZEN_IDENTITIES)
LEGACY_COMMANDS = {"video.ripple", "video.otio_export", "video.otio_import"}
GLOBAL_VALUES = {"--blender", "--jobs-dir", "--transactions-dir", "--timeout", "--legacy-video-edition"}


def legacy_root(edition):
    return ROOT / "runtime" / "video-editing" / edition


def default_legacy_jobs(edition):
    return legacy_root(edition) / "runtime" / "blenderctl" / "jobs"


def _split_globals(argv):
    options = []
    values = {}
    index = 0
    while index < len(argv) and argv[index].startswith("--"):
        item = argv[index]
        flag, sep, argument = item.partition("=")
        index += 1
        if flag in GLOBAL_VALUES:
            if not sep:
                if index == len(argv) or argv[index].startswith("--"):
                    raise Failure("INVALID_REQUEST", f"{flag} requires a value")
                argument = argv[index]
                index += 1
            values[flag] = argument
            if flag == "--legacy-video-edition":
                continue
            options.append(f"{flag}={argument}")
        else:
            options.append(item)
    return options, values, argv[index:]


def _optional_object(path):
    try:
        data = read_json(path)
    except Failure as exc:
        if exc.code != "NOT_FOUND":
            raise
        return {}
    if not isinstance(data, dict):
        raise Failure("INVALID_REQUEST", f"Video record must be an object: {path}")
    return data


def _identity_edition(*identities):
    if any(identity is not None and (not isinstance(identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity)) for identity in identities):
        raise Failure("INVALID_REQUEST", "Invalid Video implementation fingerprint")
    values = {identity for identity in identities if identity is not None}
    if len(values) > 1:
        raise Failure("CONFLICT", "Video implementation fingerprints disagree")
    if not values:
        return None
    fingerprint, = values
    for edition, expected in FROZEN_IDENTITIES.items():
        if fingerprint == expected:
            return edition
    if fingerprint == implementation_sha256():
        return CURRENT_EDITION
    raise Failure("CONFLICT", "Unknown Video implementation fingerprint. Use --edition video@VERSION "
                  "with the installed matching runtime.")


def _request_params(request):
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise Failure("INVALID_REQUEST", "Video request params must be an object")
    return params


def _record_edition(path, report=None, explicit=None):
    resolved = path.resolve()
    launch = _optional_object(resolved.parent / "launch.json")
    request = _optional_object(resolved.parent / "request.json")
    if report is None:
        report = _optional_object(resolved.parent / "video-report.json")
    identity = _identity_edition(_request_params(request).get("implementation_sha256"),
                                 report.get("implementation_sha256"))
    if identity:
        if explicit is not None and explicit != identity:
            raise Failure("CONFLICT", f"Video job is pinned to {identity}; requested edition is {explicit}.")
        return identity
    if explicit is not None:
        return explicit
    for edition in LEGACY_EDITIONS:
        if resolved.is_relative_to(legacy_root(edition)) or Path(
            launch.get("transactions_root", ".")
        ).resolve().is_relative_to(legacy_root(edition)):
            return edition
    raise Failure("CONFLICT", "Video record has no implementation fingerprint or known runtime path. "
                  "Specify --edition video@VERSION.")


def _find_job(token, explicit_root):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise Failure("INVALID_REQUEST", "job ID must be 32 lowercase hex characters")
    roots = [Path(explicit_root).resolve()] if explicit_root else [
        DEFAULT_JOBS, *(default_legacy_jobs(edition) for edition in LEGACY_EDITIONS),
    ]
    found = []
    for root in roots:
        location = (root / token).resolve()
        if location.parent != root.resolve():
            raise Failure("INVALID_REQUEST", "Job path leaves jobs directory")
        request = location / "request.json"
        if request.is_file():
            command = read_json(request).get("command", "")
            if not command.startswith("video."):
                raise Failure("INVALID_REQUEST", "Requested job is not a Video Workflow job")
            found.append((root, request))
    if len(found) > 1:
        raise Failure("CONFLICT", "Job ID occurs in multiple roots; specify --jobs-dir")
    if not found:
        raise Failure("NOT_FOUND", f"Unknown Video Workflow job: {token}")
    return found[0]


def _read_option(args, name):
    for index, arg in enumerate(args):
        flag, equals, value = arg.partition("=")
        if flag == name:
            if equals:
                return value
            if index + 1 == len(args) or args[index + 1].startswith("--"):
                raise Failure("INVALID_REQUEST", f"{name} requires a path")
            return args[index + 1]
    return None


def _resume_edition(filename):
    report = Path(filename).resolve()
    data = read_json(report)
    if not isinstance(data, dict):
        raise Failure("INVALID_REQUEST", "Video resume report must be an object")
    candidate = data.get("candidate")
    if not (report.parent / "launch.json").is_file() and isinstance(candidate, dict):
        filename = candidate.get("file")
        if isinstance(filename, str) and (Path(filename).parent / "launch.json").is_file():
            report = Path(filename)
    return _record_edition(report, report=data)


def plan(argv):
    options, fields, args = _split_globals(argv)
    edition = fields.get("--legacy-video-edition")
    if edition is not None and edition not in (*LEGACY_EDITIONS, CURRENT_EDITION):
        raise Failure("INVALID_REQUEST", f"Unknown Video Workflow version: {edition}")
    if args[:2] == ["video", "job"]:
        if len(args) <= 3 and any(flag in args for flag in ("--help", "-h")):
            return (HERE / "cli.py", [*options, *args[1:]])
        if len(args) < 4:
            raise Failure("INVALID_REQUEST", "Expected video job {status,result,wait,cancel} JOB_ID")
        root, record = _find_job(args[3], fields.get("--jobs-dir"))
        edition = _record_edition(record, explicit=edition)
        if "--jobs-dir" not in fields:
            options.append(f"--jobs-dir={root}")
        args = args[1:]
    if not edition and args[:1] == ["request"] and len(args) >= 2 and Path(args[1]).is_file():
        request = read_json(args[1])
        if not isinstance(request, dict):
            raise Failure("INVALID_REQUEST", "Video request must be an object")
        if request.get("command") in LEGACY_COMMANDS:
            edition = LATEST_LEGACY
        elif request.get("command") == "video.run" and isinstance(request.get("params"), dict):
            resume = request["params"].get("resume")
            if isinstance(resume, dict) and isinstance(resume.get("file"), str):
                edition = _resume_edition(resume["file"])
        if not edition and request.get("command") in {"video.run", "video.inspect"}:
            edition = _identity_edition(_request_params(request).get("implementation_sha256"))
    if not edition and args[:2] in (["video", "ripple"], ["video", "otio"]):
        edition = LATEST_LEGACY
    if not edition and args[:2] == ["video", "describe"]:
        if _read_option(args, "--operation") in {"ripple", "otio_export", "otio_import"}:
            edition = LATEST_LEGACY
    if not edition and args[:2] == ["video", "run"]:
        resume = _read_option(args, "--resume")
        if resume:
            edition = _resume_edition(resume)
    if edition == "0.2.0" and "--jobs-dir" not in fields and not args[:1] == ["job"]:
        # The integrated 0.2.0 route wrote new jobs into the public queue.
        options.append(f"--jobs-dir={DEFAULT_JOBS}")
    entry = legacy_root(edition) / "tools/blenderctl/cli.py" if edition in LEGACY_EDITIONS else HERE / "cli.py"
    if not entry.is_file():
        raise Failure("NOT_FOUND", f"Video entry unavailable: {entry}")
    return entry, [*options, *args]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        entry, args = plan(sys.argv[1:])
    except Failure as exc:
        import json
        print(json.dumps(envelope("video", error={"code": exc.code, "message": str(exc)}), ensure_ascii=False))
        return CODES.get(exc.code, 1)
    return subprocess.run([sys.executable, "-B", str(entry), *args], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
