"""Select the scheduler that owns a saved batch before reading or changing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEV_RELEASE = "0.3.1-dev"
RECOVERY_RC1 = "0.3.1-recovery-rc1"
RECOVERY_RC2 = "0.3.1-recovery-rc2"
DELIVERY_RC2 = "1.0.0-rc2"
FROZEN_WORKER = "--internal-render-batch-frozen"
SOURCE_GUARD = "source_render_settings.py"
STABLE_GUARD_SHA256 = "67b693320b94fc14584138356f4262b04eb3d9df2d842d02fc6464b033d8faa0"
DEV_CORE_HASHES = {
    "render_batch.py": "a6e4aafd24d81da1b134c5c9feea42ebb0df210c8f1d79cbd52766c521703a95",
    "render_batch_probe.py": "1fb27f70c6a5886080feeeca6b3c63e93f4d8066e5f06247d8cd1e67720e77df",
    "rendering.py": "1d9e0099920dffc4a8dbef6340359259bab66aa2b4d8bfa742382940448d65c0",
    SOURCE_GUARD: "8bc6252b20cdc6bda2af4d60575843964337545c541c4505947052153d6f19a2",
}
FROZEN_CORE_HASHES = {
    DELIVERY_RC2: {
        "render_batch.py": "eac220d4e8299443c584b6da5c9a88310c64ce338b164cb90467a8704e79d1ad",
        "render_batch_probe.py": "9438afbdf78d7ef1e62a8b2e3b52448ba3980118832f8be4bb1a679504ac1ffe",
        "rendering.py": "1f70a3408300f7a887503ead2dabe04f6ed217f9575286688670cf5be4414373",
        "source_render_settings.py": "6c03621e6b2aa65580ce7957cbda7602a17d0d924e2adbe1fd10869b2de1a0a0",
        "filesystem.py": "d8cb1f07feb9dac590ea1103d7d6c0a8c0fcdca6377c49b57cce07e0369bed0e",
        "runner.py": "ee4a7b28798d6d8b91c13002627709892ff6a75480ae8493cd6d9172f6555bc8",
        "worker.py": "22fd678876003bdfbf463b47d81e7ed873fbac9bffcdb3a4bc2a5f8fcfb73889",
    },
    DEV_RELEASE: DEV_CORE_HASHES,
    RECOVERY_RC1: {
        **DEV_CORE_HASHES,
        "render_batch.py": "5d20b81fe1c096cb47af96af9f45214611f24c19be31c450b5cdf1e03fe088ae",
        "filesystem.py": "16d89a374993bc25c8010ed62767172228f2c8552ed5f4e68ab6f0949d533761",
    },
    RECOVERY_RC2: {
        **DEV_CORE_HASHES,
        "render_batch.py": "cb4a6bc0a85e6c3c693e9ad6847d0a2c982065826c28e07ec30d76f6ce8f2484",
        "filesystem.py": "16d89a374993bc25c8010ed62767172228f2c8552ed5f4e68ab6f0949d533761",
        "render_batch_probe.py": "4116e785aba3d0ca89fabd602a0f776a9103ce1dcd0c45ca6ef058e30ef0f53a",
        "runner.py": "d8d9839f9968c9c3f806c64b1bb03cbc874ac7ba18d80a328c59e328de6584ce",
        "worker.py": "f96fd83946af33e89d9817ac91fdb0f4c442a24afffef5491364e59fcc45f5c3",
    },
}
FROZEN_TREE_HASHES = {
    DELIVERY_RC2: "c747a687644e0c335d9e434b2a2bdd28e99b85be411a08931970a50616f42673",
    DEV_RELEASE: "b761a5adbcacf99148c0c6ebe8c767d0c2235b19b29176baf650dc3667047258",
    RECOVERY_RC1: "ae9bfd46ce606ffabf7a66e00f6237a6ab36f7a482e7c0f993d18570e5ea3c60",
    RECOVERY_RC2: "81eee4cae6e9709c7bc07570a7f95cd08b543f185214554650926a8087d908ae",
}
IS_FROZEN_WORKER = sys.argv[1:2] == [FROZEN_WORKER]
if IS_FROZEN_WORKER and (len(sys.argv) < 5 or sys.argv[2] not in FROZEN_TREE_HASHES):
    raise SystemExit("Unrecognized frozen batch worker release")
PORTABLE_RELEASE = sys.argv[2] if IS_FROZEN_WORKER else "0.3.0"
PORTABLE = (ROOT / "runtime/render-batch" / PORTABLE_RELEASE if IS_FROZEN_WORKER
            else ROOT / "opensource/blender-render-batch")
# Each worker, including its later Blender subprocesses, imports one runtime tree.
sys.path.insert(0, str(PORTABLE / "runtime/tools/blenderctl"))
sys.path.insert(1, str(PORTABLE / "runtime/tools"))
import render_batch as portable
from protocol import CODES, Failure, atomic_json, digest, envelope, read_json
from render_batch_media import preflight_tools

# These identities were frozen by runner in pipeline-runtime.json. The parent
# path may differ if a portable workspace moved between machines.
SCHEDULER_HASHES = {
    "d80397a0bc8b4376d9bc2f8e049ce3355c0004c7d94e0a4c4e09e32cbf4b28ec": "0.2.1",
    "bde485e2b93bf780079761b054e04ca799937dcf0c32247d63e9d715e5bbc3c8": "0.2.2",
    "a6e4aafd24d81da1b134c5c9feea42ebb0df210c8f1d79cbd52766c521703a95": "0.3.0",
    FROZEN_CORE_HASHES[RECOVERY_RC1]["render_batch.py"]: RECOVERY_RC1,
    FROZEN_CORE_HASHES[RECOVERY_RC2]["render_batch.py"]: RECOVERY_RC2,
    FROZEN_CORE_HASHES[DELIVERY_RC2]["render_batch.py"]: DELIVERY_RC2,
}


def check_frozen_worktree(release):
    worktree = ROOT / "runtime/render-batch" / release
    tools = worktree / "runtime/tools"
    entry = worktree / "batch_cli.py"
    if not tools.is_dir() or not entry.is_file():
        raise Failure("CONFLICT", f"Missing local batch runtime {release}: {worktree}")
    files = [entry, *(path for path in tools.rglob("*") if path.is_file()
                      and "__pycache__" not in path.parts and path.suffix != ".pyc")]
    members = {path.relative_to(worktree).as_posix(): digest(path) for path in files}
    encoded = json.dumps(members, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != FROZEN_TREE_HASHES[release]:
        raise Failure("CONFLICT", f"Installed batch runtime differs from release {release}: {worktree}")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def historical_release(batch, status):
    releases = set()
    for shot in status["shots"]:
        for attempt in shot["attempts"]:
            ident = attempt["job_id"]
            if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise Failure("CONFLICT", "Invalid child pipeline identity in saved status")
            job = batch / "j" / ident
            receipt = job / "pipeline-runtime.json"
            if not receipt.is_file():
                continue  # The child may still be starting.
            if read_json(job / "status.json").get("job_id") != ident:
                raise Failure("CONFLICT", f"Child pipeline status belongs to another job: {job}")
            hashes = read_json(receipt)["hashes"]
            matches = [(Path(name), value) for name, value in hashes.items() if Path(name).name == "render_batch.py"]
            if len(matches) != 1 or matches[0][1] not in SCHEDULER_HASHES:
                raise Failure("CONFLICT", f"Unrecognized scheduler implementation in {receipt}")
            scheduler, fingerprint = matches[0]
            release = SCHEDULER_HASHES[fingerprint]
            if release == "0.3.0":
                guards = [(Path(name), value) for name, value in hashes.items()
                          if Path(name).name.casefold() == SOURCE_GUARD]
                if (len(guards) != 1 or guards[0][0].parent != scheduler.parent
                        or guards[0][1] not in (STABLE_GUARD_SHA256, DEV_CORE_HASHES[SOURCE_GUARD])):
                    raise Failure("CONFLICT", "Cannot identify 0.3.0 vs 0.3.1-dev without a unique known source-render "
                                  f"guard in {receipt}. Recover the original release marker or the complete child receipt.")
                if guards[0][1] == DEV_CORE_HASHES[SOURCE_GUARD]:
                    release = DEV_RELEASE
            expected = FROZEN_CORE_HASHES.get(release)
            if expected is not None:
                core = {Path(name).name: value for name, value in hashes.items()
                        if Path(name).parent == scheduler.parent and Path(name).name in expected}
                if core != expected:
                    raise Failure("CONFLICT", f"Incomplete {release} batch identity in {receipt}")
            releases.add(release)
    if len(releases) > 1:
        raise Failure("CONFLICT", "Child pipeline receipts identify different batch schedulers")
    return next(iter(releases), None)


def saved_release(batch, requested):
    receipt = batch / "batch-runtime.json"
    if receipt.exists():
        marker = read_json(receipt)
        if (not isinstance(marker, dict) or marker.get("version") != "1.0"
                or marker.get("batch_id") != batch.name
                or marker.get("revision") not in ("0.2.2", "0.3.0", *FROZEN_CORE_HASHES)):
            raise Failure("CONFLICT", f"Unrecognized batch runtime receipt: {receipt}")
        release = marker["revision"]
        if release in FROZEN_CORE_HASHES and marker.get("implementation") != FROZEN_CORE_HASHES[release]:
            raise Failure("CONFLICT", f"Frozen {release} batch runtime identity differs: {receipt}")
    else:
        status = read_json(batch / "status.json")
        if status["batch_id"] != batch.name:
            raise Failure("CONFLICT", "Saved status belongs to another batch")
        release = historical_release(batch, status)
        if not release:
            if requested in ("0.2.1", "0.3.0"):
                return requested
            raise Failure("CONFLICT", "No release receipt or identifiable child pipeline yet. "
                          "Specify the original installed edition, for example --edition render-batch@0.3.0.")
    if requested != "auto" and release != requested:
        raise Failure("CONFLICT", f"Batch belongs to {release}, requested {requested}")
    return release


def submit_portable(args, release):
    """Persist the release before the portable freezer or supervisor exposes a job."""
    directory = args.jobs_dir.resolve() / "b"
    portable.require(os.name == "nt", "Local batch worker currently uses Windows file guards")
    manifest = portable.normalize_batch(read_json(args.manifest))
    portable.require(30 <= args.timeout <= 86400, "Use a pipeline timeout from 30 to 86400 seconds")
    config = {"manifest": manifest, "blender": str(portable.absolute_file(args.blender.resolve())),
              "ffmpeg": str(portable.absolute_file(args.ffmpeg)), "ffprobe": str(portable.absolute_file(args.ffprobe)),
              "transactions_dir": str(args.transactions_dir.resolve()), "timeout": args.timeout}
    if release == DELIVERY_RC2:
        config["runtime_release"] = release
    directory.mkdir(parents=True, exist_ok=True)
    try:
        preflight_tools(config["ffmpeg"], config["ffprobe"], directory)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise Failure("UNSUPPORTED", str(exc)) from exc

    batch = directory / uuid.uuid4().hex[:16]
    batch.mkdir()
    status = {"version": "1.0", "batch_id": batch.name, "state": "freezing", "message": "批次已接收",
              "review": None, "shots": [
                  {"id": shot["id"], "state": "queued", "message": "等待", "completed_frames": 0,
                   "total_frames": shot["frame_end"] - shot["frame_start"] + 1, "preview": None,
                   "final": None, "attempts": []} for shot in manifest["shots"]]}
    with portable.PathLocks([batch]):
        if release == DELIVERY_RC2:
            status["runtime_release"] = release
        marker = {"version": "1.0", "revision": release, "batch_id": batch.name}
        if release in FROZEN_CORE_HASHES:
            marker["implementation"] = FROZEN_CORE_HASHES[release]
        atomic_json(batch / "batch-runtime.json", marker)
        atomic_json(batch / "launch.json", config)
        portable.freeze_batch(batch, status, manifest)
    if status["state"] not in ("failed", "paused"):
        batch, status = portable.invoke(batch, status, args.background)
    return envelope("render.batch.submit", batch.name, status,
                    artifacts={"batch_directory": str(batch), "status": str(batch / "status.json")})


def main(host, requested, argv):
    parser = Parser(description=__doc__)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--transactions-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--async", dest="background", action="store_true")
    for flag in ("human", "compact", "full"):
        parser.add_argument("--" + flag, action="store_true")
    render = parser.add_subparsers(required=True).add_parser("render")
    portable.register_parser(render.add_subparsers(required=True))
    action = None
    args = None
    try:
        args = parser.parse_args(argv)
        action = "render.batch." + args.batch_action
        if not re.fullmatch(r"\d+\.\d+\.\d+", host) or tuple(map(int, host.split("."))) < (5, 3, 0):
            raise Failure("UNSUPPORTED", "Render Batch requires Windows Blender 5.3.0 or later.")
        if args.batch_action == "submit":
            if requested == "auto":
                registry_file = ROOT / "tools/blenderctl/registry.json"
                release = json.loads(registry_file.read_text(encoding="utf-8"))["launcher"]["new_jobs"]["render-batch"]
            else:
                release = requested
            if release == "0.2.1":
                raise Failure("INVALID_REQUEST", "0.2.1 is available for recovery of existing batches")
        else:
            if not portable.BATCH_ID.fullmatch(args.batch_id):
                raise Failure("INVALID_REQUEST", "Invalid batch id")
            batch = args.jobs_dir.resolve() / "b" / args.batch_id
            release = saved_release(batch, requested)
            if args.batch_action == "resume":
                frozen_host = Path(read_json(batch / "launch.json")["blender"])
                if not frozen_host.is_file() or not frozen_host.samefile(args.blender):
                    raise Failure("CONFLICT", "This batch was frozen with a different Blender executable. "
                                  "Resume with its original Windows Blender 5.3.0 executable, "
                                  "or submit a new 5.3.0 batch for an older project.")
        if IS_FROZEN_WORKER and release != PORTABLE_RELEASE:
            raise Failure("CONFLICT", "Frozen batch worker received a different release")
        if release in FROZEN_CORE_HASHES:
            check_frozen_worktree(release)
            if not IS_FROZEN_WORKER:
                return subprocess.call([sys.executable, "-B", str(Path(__file__).resolve()), FROZEN_WORKER,
                                        release, host, release, *argv])
        else:
            if release != PORTABLE_RELEASE:
                raise Failure("UNSUPPORTED", f"Render Batch {release} is an archived release")
            core = portable.CLI_DIR
            if SCHEDULER_HASHES.get(digest(core / "render_batch.py")) != release:
                raise Failure("CONFLICT", f"Installed batch scheduler differs from release {release}: {core}")
        if release == "0.3.0" and digest(core / SOURCE_GUARD) != STABLE_GUARD_SHA256:
            raise Failure("CONFLICT", f"Installed source-render guard differs from release 0.3.0: {core}")
        if args.compact or args.full:
            raise Failure("INVALID_REQUEST", "Batch status already has a bounded public projection")
        result = submit_portable(args, release) if args.batch_action == "submit" else portable.dispatch(args)
        code = 4 if args.batch_action == "submit" and result["data"]["state"] in ("failed", "partial") else 0
    except (Failure, OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
        kind = exc.code if isinstance(exc, Failure) else "IO_ERROR" if isinstance(exc, OSError) else "INVALID_REQUEST"
        result = envelope(action, error={"code": kind, "message": str(exc)})
        code = CODES[kind]
    if args and args.human:
        print(f"{'OK' if result['ok'] else 'ERROR'} {action} job={result['job_id'] or '-'}")
        print(json.dumps(result["data"] if result["ok"] else result["error"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    arguments = sys.argv[3:] if IS_FROZEN_WORKER else sys.argv[1:]
    sys.exit(main(arguments[0], arguments[1], arguments[2:]))
