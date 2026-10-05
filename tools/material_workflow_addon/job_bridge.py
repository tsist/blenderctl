# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, reloadable GUI client for the public blenderctl job protocol."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess

_CONFIGS = {}
_COMMANDS = {"material.run", "material.batch", "doctor"}
_TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class BridgeError(RuntimeError):
    def __init__(self, message, envelope=None):
        super().__init__(message)
        self.envelope = envelope


def _path(value, exists=False):
    p = Path(value)
    if not p.is_absolute():
        raise BridgeError("An absolute path is required: " + str(value))
    p = p.resolve()
    if exists and not p.is_file():
        raise BridgeError("Required file does not exist: " + str(p))
    return p


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        raise BridgeError("Cannot read JSON: " + str(path)) from exc


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _process(args, cwd, seconds):
    try:
        return subprocess.run(args, cwd=str(cwd), shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                              if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("CLI response timed out; submission outcome may be unknown. Preserve the handoff directory and inspect jobs before retrying.") from exc
    except OSError as exc:
        raise BridgeError("Cannot start CLI: " + str(exc)) from exc


def configure(project_root, blender_binary, python_binary=None):
    root = _path(project_root)
    blender = _path(blender_binary, True)
    cli = _path(root / "tools/blenderctl/cli.py", True)
    if python_binary is None:
        matches = list(blender.parent.glob("[0-9]*.[0-9]*/python/bin/python.exe"))
        if len(matches) != 1:
            raise BridgeError("Cannot uniquely locate Blender Python; specify python_binary")
        python_binary = matches[0]
    python = _path(python_binary, True)
    key = (str(root), str(blender), str(python))
    if key not in _CONFIGS:
        response = _process([str(python), str(cli), "--version"], root, 10)
        version = response.stdout.strip()
        if response.returncode or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise BridgeError("CLI version check failed: " + (response.stderr or version)[:400])
        _CONFIGS[key] = dict(project_root=str(root), blender_binary=str(blender),
                            python_binary=str(python), cli_file=str(cli), cli_version=version)
    return dict(_CONFIGS[key])


def _call(config, jobs, tail, timeout=300):
    if not isinstance(config, dict) or not {"project_root", "blender_binary", "python_binary"}.issubset(config):
        raise BridgeError("A validated backend configuration is required")
    checked = configure(config["project_root"], config["blender_binary"], config["python_binary"])
    if config != checked:
        raise BridgeError("Configuration does not match the validated CLI backend")
    args = [checked["python_binary"], checked["cli_file"], "--blender", checked["blender_binary"],
            "--jobs-dir", str(jobs), "--timeout", str(timeout), *tail]
    response = _process(args, checked["project_root"], 15)
    try:
        value = json.loads(response.stdout)
    except ValueError as exc:
        raise BridgeError("CLI did not return JSON (exit %s): %s" %
                          (response.returncode, (response.stderr or response.stdout)[:600])) from exc
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        raise BridgeError("CLI returned an invalid envelope")
    if response.returncode and value["ok"]:
        raise BridgeError("CLI exited unsuccessfully despite an ok envelope", value)
    # A failed public job.result still carries the authoritative result envelope.
    return value


def submit(request_file, jobs_root, config, timeout=300):
    request_file = _path(request_file, True)
    jobs = _path(jobs_root)
    request = _read(request_file)
    if request.get("command") not in _COMMANDS:
        raise BridgeError("Only material workflows and doctor may be submitted")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError("Job timeout must be a positive finite number")
    value = _call(config, jobs, ["--async", "request", str(request_file)], timeout)
    if not value["ok"]:
        raise BridgeError(str(value.get("error")), value)
    if value.get("command") != "job.submit":
        raise BridgeError("CLI did not acknowledge job.submit", value)
    ident = value.get("job_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise BridgeError("CLI returned an invalid submitted job ID", value)
    job = (jobs / ident).resolve()
    if job.parent != jobs:
        raise BridgeError("Submitted job escapes jobs root", value)
    normalized = _read(job / "request.json")
    if normalized.get("command") != request["command"]:
        raise BridgeError("Submitted command does not match the handoff", value)
    ref = dict(job_id=value.get("job_id"), jobs_dir=str(jobs), request_file=str(request_file),
               request_sha256=_sha(normalized), submitted_request_sha256=_sha(request), command=request["command"],
               blender_binary=config["blender_binary"], project_root=config["project_root"])
    _owned(ref, config)
    value["job_ref"] = ref
    return value


def _owned(ref, config):
    if not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(ref.get("job_id", ""))):
        raise BridgeError("Invalid owned job ID")
    jobs = _path(ref.get("jobs_dir", ""))
    job = (jobs / ref["job_id"]).resolve()
    if job.parent != jobs or not job.is_dir():
        raise BridgeError("Job directory is missing or escapes its owned jobs root")
    if any((job / name).resolve().parent != job for name in ("request.json", "launch.json")):
        raise BridgeError("Job metadata escapes its owned job directory")
    request = _read(job / "request.json")
    launch = _read(job / "launch.json")
    if (ref.get("command") not in _COMMANDS or request.get("command") != ref["command"] or
            _sha(request) != ref.get("request_sha256") or
            ref.get("project_root") != config["project_root"] or
            ref.get("blender_binary") != config["blender_binary"] or
            str(_path(launch.get("blender", ""))) != config["blender_binary"]):
        raise BridgeError("Job ownership does not match this handoff/backend")
    return jobs


def _job(action, job_ref, config):
    jobs = _owned(job_ref, config)
    value = _call(config, jobs, ["job", action, job_ref["job_id"]])
    if value.get("job_id") not in (None, job_ref["job_id"]):
        raise BridgeError("CLI returned a different job ID", value)
    return value


def status(job_ref, config):
    return _job("status", job_ref, config)


def result(job_ref, config):
    return _job("result", job_ref, config)


read_result = result


def _normalized_request(request_file, config):
    """Use the actual host adapters without importing their modules into Blender."""
    request = _read(request_file)
    if request.get("schema_version") != "1.0" or set(request) - {"schema_version", "command", "params"}:
        raise BridgeError("Invalid recovery request envelope")
    command = request.get("command")
    if command == "doctor":
        if request.get("params", {}) != {}:
            raise BridgeError("Recovery doctor requires empty params")
        return {"schema_version": "1.0", "command": "doctor", "params": {}}
    if command not in {"material.run", "material.batch"}:
        raise BridgeError("Unsupported recovery command")
    # Fixed code plus absolute path argv, no shell and no job creation.
    code = ("import json,sys;from pathlib import Path;"
            "sys.path.insert(0,str(Path(sys.argv[1])/'tools'/'blenderctl'));"
            "import material_workflow_contract as single;import material_batch_contract as batch;"
            "r=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8-sig'));"
            "n=(single if r['command']=='material.run' else batch).normalize_params(r.get('params',{}));"
            "print(json.dumps({'schema_version':'1.0','command':r['command'],'params':n},ensure_ascii=False,allow_nan=False))")
    response = _process([config["python_binary"], "-c", code, config["project_root"], str(request_file)],
                        config["project_root"], 15)
    if response.returncode:
        raise BridgeError("Host request normalization failed: " + response.stderr[-600:])
    try:
        value = json.loads(response.stdout)
    except ValueError as exc:
        raise BridgeError("Host normalization did not return JSON") from exc
    if not isinstance(value, dict) or value.get("command") != command:
        raise BridgeError("Host normalization returned an invalid request")
    return value


def recover(request_file, jobs_root, config, exclude_job_ids=()):
    """Find exactly one matching existing job; never submit or infer its outcome."""
    request_file = _path(request_file, True)
    jobs = _path(jobs_root)
    checked = configure(config["project_root"], config["blender_binary"], config["python_binary"])
    if config != checked:
        raise BridgeError("Configuration does not match the validated CLI backend")
    normalized = _normalized_request(request_file, config)
    excluded=set(exclude_job_ids)
    if any(not isinstance(i,str) or not re.fullmatch(r'[0-9a-f]{32}',i) for i in excluded):
        raise BridgeError('Invalid previous attempt ID')
    digest = _sha(normalized)
    if not jobs.is_dir():
        raise BridgeError("No matching existing job; submission remains unknown")
    matches = []
    for directory in jobs.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            continue
        if directory.name in excluded:
            continue
        job = directory.resolve()
        if job.parent != jobs or not job.is_dir():
            raise BridgeError("Recovery job directory escapes its jobs root")
        for name in ("request.json", "launch.json"):
            if (job / name).resolve().parent != job:
                raise BridgeError("Recovery metadata escapes its job directory")
        try:
            candidate = _read(job / "request.json")
            launch = _read(job / "launch.json")
        except BridgeError:
            # A partially-created job makes uniqueness uncertain. Refuse instead of ignoring.
            raise BridgeError("Incomplete job metadata; submission remains unknown: " + job.name)
        if candidate != normalized or _sha(candidate) != digest:
            continue
        if str(_path(launch.get("blender", ""))) != config["blender_binary"]:
            continue
        matches.append(dict(job_id=job.name, jobs_dir=str(jobs), request_file=str(request_file),
                            request_sha256=digest, submitted_request_sha256=_sha(_read(request_file)),
                            command=normalized["command"], blender_binary=config["blender_binary"],
                            project_root=config["project_root"]))
    if len(matches) != 1:
        raise BridgeError("Recovery requires exactly one matching job; found %d. No new job submitted." % len(matches))
    _owned(matches[0], config)
    return matches[0]


def cancel(job_ref, config):
    return _job("cancel", job_ref, config)


def summarize(envelope):
    """Present facts without turning cancellation requests or partial files into success."""
    data = envelope.get("data") or {}
    state = data.get("state")
    return dict(state=state, terminal=state in _TERMINAL,
                cancel_requested=bool(data.get("cancel_requested", False)),
                ok=envelope.get("ok"), partial=bool(data.get("partial", False)),
                error=envelope.get("error"))
