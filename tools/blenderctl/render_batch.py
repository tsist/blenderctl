"""Local shot batches backed by protected blenderctl render jobs."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from filesystem import FileGuard, PathLocks
from protocol import SCHEMA, VERSION, Failure, atomic_json, digest, envelope, read_json
from render_batch_resources import disk_full, frame_space, gpu_sample, memory_admission, space_shortfall
from render_batch_delivery import PROFILES, dimensions, frame_bytes, frame_rate, hash_frames
from render_batch_delivery import normalize_shot_delivery
from render_contract import normalize_run
from resource_leases import process_identity
import runner

TERMINAL = {"completed", "partial", "failed", "paused"}
HEX = re.compile(r"[a-f0-9]{64}\Z")
SHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
BATCH_ID = re.compile(r"[a-f0-9]{16}\Z")
CLI_DIR = Path(__file__).resolve().parent
SEGMENT_FRAMES = 96
MAX_SHOT_FRAMES = SEGMENT_FRAMES * 32
SUPERVISOR_GRACE = 20


class Paused(Exception):
    pass


class DiskPaused(Paused):
    pass


def check_disk(directory, reserve, additional, *, ran_out=False):
    deficit = space_shortfall(directory, reserve, additional)
    if deficit or ran_out:
        amount = f"预计还差 {deficit} MiB" if deficit else "写入时磁盘空间耗尽，请释放空间后继续"
        raise DiskPaused(f"{amount}。冻结输入和已验帧已保留。")


def register_parser(render_actions):
    actions = render_actions.add_parser("batch", help="ordered shots, review video and render receipts")
    sub = actions.add_subparsers(dest="batch_action", required=True)
    submit = sub.add_parser("submit", help="freeze the inputs and start a local batch")
    submit.add_argument("--manifest", type=Path, required=True)
    submit.add_argument("--ffmpeg", type=Path, required=True)
    submit.add_argument("--ffprobe", type=Path, required=True)
    sub.add_parser("status", help="read the same record as the Blender panel").add_argument("batch_id")
    wait = sub.add_parser("wait", help="wait for terminal status or a bounded progress snapshot")
    wait.add_argument("batch_id")
    wait.add_argument("--wait-seconds", type=int, default=30)
    sub.add_parser("cancel", help="pause this batch and retain verified frames").add_argument("batch_id")
    sub.add_parser("resume", help="continue a paused or incomplete batch on the frozen inputs").add_argument("batch_id")


def require(condition, message):
    if not condition:
        raise Failure("INVALID_REQUEST", message)


def fields(data, *, required, optional=()):
    require(isinstance(data, dict) and set(required) <= data.keys()
            and data.keys() <= set(required) | set(optional), "Missing or unknown batch fields")
    return data


def absolute_file(value):
    require(isinstance(value, (str, Path)), "Expected an absolute file path")
    path = Path(value)
    require(path.is_absolute() and path.is_file(), f"Missing absolute file: {path}")
    require(not path.is_symlink() and path.resolve() == path,
            f"Use a real canonical file path: {path}")
    return path


def relative_name(value):
    require(isinstance(value, str), "Expected a relative file path")
    path = Path(value)
    require(not path.anchor and path.parts and all(
        part not in {".", ".."} and ":" not in part for part in path.parts
    ), f"Unsafe relative path: {value}")
    return path


def hash_text(value):
    require(isinstance(value, str) and HEX.fullmatch(value), "Expected lowercase SHA-256")
    return value


def descriptor(data):
    fields(data, required=("file", "expected_sha256"))
    return {"file": str(absolute_file(data["file"])), "expected_sha256": hash_text(data["expected_sha256"])}


def normalize_batch(data):
    fields(data, required=("version", "backend", "shots", "delivery"),
           optional=("cpu_threads", "min_free_mb"))
    require(data["version"] == "1.0" and data["backend"] == "local", "This candidate accepts local batch 1.0")
    delivery = fields(data["delivery"], required=("width", "height", "fps", "color", "review_codec"),
                      optional=("delivery_profile",))
    dimensions((delivery['width'], delivery['height']), review=True)
    frame_rate(delivery['fps'])
    require(delivery['color'] == 'SDR' and delivery['review_codec'] == 'h264', 'Review uses SDR H.264')
    threads = data.get("cpu_threads", 8)
    require(type(threads) is int and 1 <= threads <= 8, "CPU threads must be from 1 to 8")
    min_free = data.get("min_free_mb", 4096)
    require(type(min_free) is int and 0 <= min_free <= 1048576, "Disk reserve may be 0–1048576 MiB")
    require(isinstance(data["shots"], list) and 1 <= len(data["shots"]) <= 32, "Provide 1–32 ordered shots")
    shot_ids = set()
    for shot in data["shots"]:
        fields(shot, required=("id", "project", "expected_sha256", "scene", "view_layer", "camera",
                               "frame_start", "frame_end", "settings", "device"),
               optional=("resources", "cache_manifest", "resolution", "delivery_profile", "output_passes",
                         "compositor_enabled", "exr_codec", "adaptive_sampling"))
        ident = shot["id"]
        require(isinstance(ident, str) and SHOT_ID.fullmatch(ident) and ident.casefold() not in shot_ids,
                "Each shot requires a unique short ASCII id")
        shot_ids.add(ident.casefold())
        project = absolute_file(shot["project"])
        require(project.suffix.lower() == ".blend", "Use a saved .blend file")
        shot["project"] = str(project)
        hash_text(shot["expected_sha256"])
        require(shot["settings"] == "source", "Use the source settings profile")
        normalize_shot_delivery(shot, delivery)
        for key in ("scene", "view_layer", "camera"):
            require(isinstance(shot[key], str) and 0 < len(shot[key]) <= 128, "Invalid scene, camera, or view layer")
        start, end = shot["frame_start"], shot["frame_end"]
        require(type(start) is int and type(end) is int and 1 <= start <= end <= 100000,
                "Shot frame_start and frame_end must be positive and ordered")
        require(end - start + 1 <= MAX_SHOT_FRAMES,
                f"The current runner supports at most {MAX_SHOT_FRAMES} frames per shot")
        device = fields(shot["device"], required=("backend",), optional=("id",))
        require(device["backend"] in ("CPU", "CUDA", "OPTIX", "GRAPHICS"), "Unknown device backend")
        gpu = device["backend"] in ("CUDA", "OPTIX")
        require(gpu == ("id" in device) and (not gpu or isinstance(device["id"], str) and device["id"]),
                "A Cycles GPU requires its exact Blender device id")
        resources = shot.setdefault("resources", [])
        require(isinstance(resources, list), "resources must be a list")
        for item in resources:
            fields(item, required=("path", "expected_sha256"))
            relative_name(item["path"])
            hash_text(item["expected_sha256"])
        if "cache_manifest" in shot:
            shot["cache_manifest"] = descriptor(shot["cache_manifest"])
    data["cpu_threads"] = threads
    data["min_free_mb"] = min_free
    return data


def copy_verified(source, target, expected):
    if target.exists():
        if digest(target) != expected:
            raise Failure("CONFLICT", f"Existing copy differs from the frozen input: {target}")
        return {"file": str(target), "expected_sha256": expected}
    made_hash = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".copy-{uuid.uuid4().hex}.tmp")
    try:
        with FileGuard(source) as guard, temporary.open("xb") as dest:
            for block in guard.chunks():
                dest.write(block)
                made_hash.update(block)
            dest.flush()
            os.fsync(dest.fileno())
        if made_hash.hexdigest() != expected:
            raise Failure("CONFLICT", f"Input changed before freezing: {source}")
        # Link on the same volume publishes completed bytes without replacing
        # a destination that appeared while the input was being copied.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"file": str(target), "expected_sha256": expected}


def child_path(root, relative):
    path = root / relative_name(relative)
    require(path.resolve().is_relative_to(root), "Path escapes frozen directory")
    return path


def input_files(shot, shot_dir):
    src = Path(shot["project"])
    snapshot = shot_dir / "input"
    project = (src, snapshot / src.name, shot["expected_sha256"])
    resources = [
        (child_path(src.parent, member["path"]), child_path(snapshot, member["path"]), member["expected_sha256"])
        for member in shot["resources"]
    ]
    cache_files = []
    cache = None
    if "cache_manifest" in shot:
        supplied = shot["cache_manifest"]
        source = Path(supplied["file"])
        receipt = shot_dir / "input-cache-receipt.json"
        manifest_file = receipt if receipt.exists() else source
        require(digest(manifest_file) == supplied["expected_sha256"], "The cache receipt changed before freezing")
        cache = read_json(manifest_file)
        require(isinstance(cache, dict) and cache.get("cache_manifest_version") == "1.0"
                and isinstance(cache.get("candidate"), dict)
                and isinstance(cache.get("root"), str) and isinstance(cache.get("files"), list)
                and cache["candidate"].get("sha256") == project[2],
                "Cache receipt is not bound to this project version")
        cache_src = Path(cache["root"]).resolve()
        require(cache_src.is_relative_to(src.parent) and cache_src != src.parent,
                "Cache receipt root must be below the source project directory")
        cache_dst = snapshot / cache_src.relative_to(src.parent)
        cache_files.append((source, receipt, supplied["expected_sha256"]))
        for member in cache["files"]:
            cache_files.append((child_path(cache_src, member["path"]), child_path(cache_dst, member["path"]),
                                hash_text(member["sha256"])))
    return project, resources, cache_files, cache


def freeze_inputs(shot, shot_dir, batch, reserve):
    project, resources, cache_files, cache = input_files(shot, shot_dir)
    pending_bytes = sum(source.stat().st_size for source, target, _ in (project, *resources, *cache_files)
                        if not target.is_file())
    check_disk(batch, reserve, pending_bytes)
    try:
        file = copy_verified(*project)
        copied = [copy_verified(*resource) for resource in resources]
        for member in cache_files:
            copy_verified(*member)
    except OSError as exc:
        if disk_full(exc):
            remaining = sum(source.stat().st_size for source, target, _ in (project, *resources, *cache_files)
                            if not target.is_file())
            check_disk(batch, reserve, remaining, ran_out=True)
        raise
    return {**file, "resources": copied}, cache


def probe_source(files, cache, shot, shot_dir, config):
    request = shot_dir / "source-probe.json"
    atomic_json(request, {**files, "cache": cache, "shot": shot, "fps": config['manifest']['delivery']['fps'],
                          "delivery_aspect": shot['resolution']})
    argv = [config["blender"], "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
            "--threads", str(config["manifest"]["cpu_threads"]), files["file"], "--python-exit-code", "49",
            "--python", str(CLI_DIR / "render_batch_probe.py"), "--", str(request)]
    with (shot_dir / "source-probe.log").open("wb") as log:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=180,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    if done.returncode:
        summary = (shot_dir / "source-probe.log").read_bytes()[-4096:].decode("utf-8", "replace").strip()
        raise Failure("VALIDATION_FAILED", "Source preflight failed. " + summary)
    settings = read_json(shot_dir / "source-settings.json")
    if settings["cache_manifest"]:
        files["cache_manifest"] = settings["cache_manifest"]
    return settings


def render_requests(shot, files, captured, frames, dimensions, stage):
    spec = {k: v for k, v in captured["source_settings"].items() if k != "effective"}
    width, height = dimensions
    spec.update({k: shot[k] for k in ("scene", "view_layer", "camera", "device")})
    profile = shot['delivery_profile'] if stage == 'final' else 'REVIEW_PNG8'
    fmt, depth, _ = PROFILES[profile]
    limit = min(SEGMENT_FRAMES, 512_000_000 // (width * height))
    overrides = {'adaptive_sampling': shot['adaptive_sampling']} if 'adaptive_sampling' in shot else {}
    for index in range(0, len(frames), limit):
        part = frames[index:index + limit]
        manifest = normalize_run({**spec, "frames": part, "width": width, "height": height,
                                  "format": fmt, "depth": depth, "passes": [], "compositor": shot['compositor_enabled'],
                                  "output_passes": shot['output_passes'] if profile == 'MASTER_EXR32' else [],
                                  "exr_codec": shot['exr_codec'], **overrides,
                                  "settings_mode": "SOURCE_C5", "pixel_frame_budget": width * height * len(part)})
        yield {**files, "manifest": manifest}


def write_status(batch, status):
    status["updated_unix"] = time.time()
    atomic_json(batch / "status.json", status)


def pipeline_steps(requests, config):
    gpu = requests[0]["manifest"]["device"]["backend"] != "CPU"
    threads = config["manifest"]["cpu_threads"]
    limits = {"cpu_threads": threads, "memory_mb": 12288, "disk_mb": 4096, "gpu_mb": 8192 if gpu else 0}
    return {"pipeline_version": "1.0",
            "policy": {**limits, "disk_mb": min(65536, len(requests) * limits["disk_mb"]),
                       "max_workers": 1, "min_free_mb": config["manifest"].get("min_free_mb", 4096),
                       "failure": "continue"},
            "steps": [{"id": f"segment{index:04d}", "command": "render.run", "params": part,
                       "depends_on": [], "bindings": [], "limits": limits, "timeout_seconds": config["timeout"],
                       "reuse": False} for index, part in enumerate(requests)]}


def progress_for(job):
    path = job / "pipeline-state.json"
    if not path.is_file():
        return 0
    pending = read_json(path)
    progress = 0
    for row in pending["steps"]:
        if row.get("child_directory") and row["state"] in ("running", "succeeded"):
            receipts = Path(row["child_directory"]) / "render-frame-receipts"
            if receipts.is_dir():
                progress += sum(1 for file in receipts.glob("*.json") if file.is_file())
    return progress


def run_pipeline(batch, status, row, requests, config, previous):
    """Run ordered render segments through the existing single-job runner."""
    report={'steps':[],'error':None}
    for params in requests:
        if (batch/'cancel.request').is_file():
            break
        job = runner.prepare({'schema_version':SCHEMA,'command':'render.run','params':params},
                             Path(config['blender']),batch/'j',config['timeout'],Path(config['transactions_dir']))
        attempt={'stage':row['state'],'job_id':job.name,'kind':'render.run'}
        row['attempts'].append(attempt)
        row['active_pipeline']=job.name
        write_status(batch,status)
        with (job/'supervisor.log').open('ab') as log:
            process=subprocess.Popen([sys.executable,'-B',str(Path(runner.__file__).resolve()),str(job)],
                                     stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        attempt['supervisor']=process_identity(process.pid)
        write_status(batch,status)
        segment=wait_render_worker(batch,status,row,job,previous,owner=attempt['supervisor'],process=process)
        report['steps'].extend(segment['steps'])
        previous+=len(list((job/'render-frame-receipts').glob('*.json')))
    return report


def wait_render_worker(batch,status,row,job,previous,*,owner=None,process=None):
    """Wait for verified frame receipts and retain an interrupted segment for resume."""
    cancel_sent=False
    while (process is not None and process.poll() is None) or (process is None and owner and process_identity(owner['pid'])==owner):
        if (batch/'cancel.request').is_file() and not cancel_sent:
            (job/'cancel.request').touch(exist_ok=True)
            cancel_sent=True
        count=min(row['total_frames'],previous+len(list((job/'render-frame-receipts').glob('*.json'))))
        if count!=row['completed_frames']:
            row['completed_frames']=count
            write_status(batch,status)
        time.sleep(.75)
    row['active_pipeline']=None
    write_status(batch,status)
    path=job/'result.json'
    if not path.is_file():
        return {'steps':[{'state':'cancelled','child_directory':str(job)}],'error':None}
    result=read_json(path)
    error=result.get('error')
    return {'steps':[{'state':'succeeded' if result['ok'] else 'failed','child_directory':str(job),'error':error}],
            'error':None}


def wait_pipeline(batch, status, row, job, previous, *, owner=None, process=None):
    cancel_sent = False
    while True:
        if (batch / "cancel.request").is_file() and not cancel_sent:
            (job / 'cancel.request').touch(exist_ok=True)
            cancel_sent = True
        if process is not None and process.poll() is not None:
            break
        if process is None and owner is not None and process_identity(owner['pid']) != owner:
            break
        if process is None and owner is None:
            if (job / 'result.json').is_file():
                break
            saved = read_json(job / 'status.json')
            pid = saved.get('supervisor_pid')
            if not pid or process_identity(pid) is None:
                break
        # The process identity, not stale heartbeat age, establishes liveness.
        # Poll only immutable verified frame receipts, avoiding state-file write contention.
        receipts = list(job.parent.glob('*/render-frame-receipts/*.json'))
        child_ids = job_step_ids(job) if receipts else set()
        completed = sum(1 for file in receipts if file.parent.parent.name in child_ids)
        if completed:
            count = min(row['total_frames'], previous + completed)
            if count != row['completed_frames']:
                row['completed_frames'] = count
                write_status(batch, status)
        time.sleep(1)
    row["active_pipeline"] = None
    write_status(batch, status)
    if not (job / 'result.json').is_file():
        return recover_pipeline(job)
    result = read_json(job / "result.json")
    report = job / "pipeline-report.json"
    if not report.is_file():
        raise Failure("WORKER_FAILED", str(result.get("error") or "No final pipeline report"))
    return read_json(report)


def job_step_ids(job):
    state_file = job / 'pipeline-state.json'
    try:
        return {row['child_job'] for row in read_json(state_file)['steps'] if row.get('child_job')}
    except (Failure, OSError):
        return set()


def recover_pipeline(job):
    """Fence a dead pipeline and expose receipts from its verified child jobs."""
    recovery = job / "batch-recovery.json"
    # The pipeline holds this same mutex until all of its worker threads stop.
    # A heartbeat alone is not evidence of death, e.g. a long disk scan.
    with PathLocks([job]):
        if recovery.is_file():
            return read_json(recovery)
        if (job / "result.json").is_file() and (job / "pipeline-report.json").is_file():
            return read_json(job / "pipeline-report.json")
        state = read_json(job / "status.json")
        supervisor = state.get("supervisor_pid")
        if supervisor and process_identity(supervisor) is not None:
            raise Failure("CONFLICT", f"Cannot confirm old supervisor is stopped: {job.name}")
        children = job / "children"
        if children.is_dir():
            for child in children.iterdir():
                if not child.is_dir():
                    continue
                state_path = child / "status.json"
                child_state = read_json(state_path) if state_path.is_file() else {}
                worker = child_state.get("process_tree_root_pid")
                if worker and process_identity(worker) is not None:
                    raise Failure("CONFLICT", f"Old render worker can still write: {child.name}")
                if not worker and (child / "worker.gate").exists():
                    raise Failure("CONFLICT", f"Old render worker has no recorded identity: {child.name}")
        # A supervisor starting late must observe cancellation before dispatch.
        (job / "cancel.request").touch(exist_ok=True)
        snapshot_path = job / "pipeline-state.json"
        snapshot = read_json(snapshot_path) if snapshot_path.is_file() else {"steps": []}
        recovered = []
        for item in snapshot.get("steps", []):
            original = item.get("child_directory")
            if not original:
                continue
            child = Path(original).resolve()
            if child.parent != children.resolve() or not child.is_dir():
                raise Failure("CONFLICT", "Recovery receipt points outside the owned pipeline")
            if not (child / "request.json").is_file() or read_json(child / "request.json").get("command") != "render.run":
                raise Failure("CONFLICT", "Recovery receipt is not from a render job")
            recovered.append({**item, "state": "cancelled"})
        report = {"steps": recovered, "error": {"code": "WORKER_FAILED", "message": "Recovered a stopped render pipeline"}}
        atomic_json(recovery, report)
        return report


def good_outputs(report, frames, directory, dimensions, jobs, suffix):
    kept = {}
    errors = [report["error"]] if report.get("error") else []
    scope = set(frames)
    for row in report["steps"]:
        folder = row.get("child_directory")
        if row["state"] != "succeeded":
            errors.append(row.get("error") or {"message": row["state"]})
        if not folder:
            continue
        folder = Path(folder).resolve()
        if not folder.is_relative_to(jobs):
            raise Failure("CONFLICT", "Pipeline receipt points outside the batch jobs directory")
        if row["state"] == "succeeded":
            rows = read_json(folder / "render-report.json")["outputs"]
        elif (row["state"] in ("cancelled", "timed_out")
              or (row.get("error") or {}).get("code") in ("WORKER_FAILED", "VALIDATION_FAILED", "RESOURCE_LIMIT")):
            rows = [read_json(file) for file in (folder / "render-frame-receipts").glob("*.json")]
        else:
            continue
        for output in rows:
            frame = output["frame"]
            image = output["images"][0]
            source = Path(output["file"])
            if (frame not in scope or frame in kept or source.parent != folder
                    or source.name != f"render-{frame:06d}{suffix}" or not source.is_file()
                    or (image["width"], image["height"]) != dimensions or image["infinite_values"]):
                continue
            name = directory / f"frame-{frame:06d}{suffix}"
            with FileGuard(source) as guard:
                if guard.sha256() != output["sha256"]:
                    continue
            if name.exists() and digest(name) != output["sha256"]:
                preserve_old(name)
            if not name.exists():
                copy_verified(source, name, output["sha256"])
            kept[frame] = {"path": str(name), "sha256": output["sha256"]}
            if suffix == '.exr':
                review = output.get('review', {})
                producer = folder / f'review-{frame:06d}.png'
                if review.get('file') != str(producer) or not producer.is_file() or digest(producer) != review.get('sha256'):
                    kept.pop(frame)
                    continue
                thumbnail = directory / f'frame-{frame:06d}.png'
                if thumbnail.exists() and digest(thumbnail) != review['sha256']:
                    preserve_old(thumbnail)
                if not thumbnail.exists():
                    copy_verified(producer, thumbnail, review['sha256'])
                kept[frame]['review'] = {'path': str(thumbnail), 'sha256': review['sha256']}
                kept[frame]['channels'] = [channel for image in output['images'] for channel in image['channels']]
                kept[frame]['color_metadata'] = [image['metadata'] for image in output['images']]
    if isinstance(report.get("error"), dict):
        errors.append(report["error"])
    return kept, errors


def preserve_old(path):
    path.rename(path.with_name(f"{path.stem}.previous-{uuid.uuid4().hex[:8]}{path.suffix}"))


def verify_saved_frames(manifest, directory, desired, captured, dimensions, suffix):
    if not manifest.is_file():
        return {}
    latest = read_json(manifest)
    if (latest.get("settings") != captured
            or (latest.get("width"), latest.get("height")) != dimensions):
        raise Failure("CONFLICT", "Stored frames belong to a different frozen render configuration")
    desired_records = []
    previews = []
    for frame in desired:
        record = latest["frames"].get(str(frame))
        expected_path = directory / f"frame-{frame:06d}{suffix}"
        if not record or Path(record['path']) != expected_path:
            continue
        desired_records.append((frame, expected_path, record))
        if suffix == '.exr':
            preview_path = directory / f'frame-{frame:06d}.png'
            preview = record.get('review', {})
            if preview.get('path') == str(preview_path):
                previews.append((frame, preview_path, preview))
    valid = hash_frames(desired_records, digest)
    if suffix == '.exr':
        valid = {frame: valid[frame] for frame in hash_frames(previews, digest) if frame in valid}
    return valid


def publish_frame_index(path, captured, dimensions, desired, frames, failures):
    missing = [frame for frame in desired if frame not in frames]
    atomic_json(path, {"state": "partial" if missing else "verified",
                       "settings": captured, "width": dimensions[0], "height": dimensions[1],
                       "frames": frames, "missing_frames": missing, "attempt_errors": failures})
    return missing


def render_phase(batch, status, row, shot, files, captured, stage, dimensions, config):
    row["state"] = stage + "_rendering"
    row["completed_frames"] = 0
    row["message"] = "正在渲染"
    write_status(batch, status)
    shot_output = batch / "shots" / ("s-" + shot["id"])
    directory = shot_output / (stage + "-frames")
    directory.mkdir(parents=True, exist_ok=True)
    desired = list(range(shot["frame_start"], shot["frame_end"] + 1))
    frame_manifest = shot_output / (stage + "-frames.json")
    profile = shot['delivery_profile'] if stage == 'final' else 'REVIEW_PNG8'
    _, depth, suffix = PROFILES[profile]
    accepted = verify_saved_frames(frame_manifest, directory, desired, captured, dimensions, suffix)
    failures = []
    prior = [a for a in row["attempts"] if a["stage"] == row["state"]]
    for attempt in prior:
        job = batch / "j" / attempt["job_id"]
        remaining = [frame for frame in desired if frame not in accepted]
        if not remaining:
            break
        recovery = job / "batch-recovery.json"
        waiter=wait_render_worker if attempt.get('kind')=='render.run' else wait_pipeline
        report = read_json(recovery) if recovery.is_file() else waiter(batch, status, row, job, len(accepted),
                                                                      owner=attempt.get('supervisor'))
        kept, errors = good_outputs(report, remaining, directory, dimensions, batch / "j", suffix)
        accepted.update(kept)
        failures.extend(errors)
        publish_frame_index(frame_manifest, captured, dimensions, desired, accepted, failures)
    reserve = config["manifest"].get("min_free_mb", 4096)
    channels = 4 * (len(captured.get('native_passes', [])) + 1) if suffix == '.exr' else 4
    bytes_per_frame = frame_bytes(dimensions, channels, depth)
    for attempt in (1, 2):
        if (batch / "cancel.request").is_file():
            raise Paused()
        remaining = [frame for frame in desired if frame not in accepted]
        if not remaining:
            break
        # A new child render and the accepted frame copy can coexist on disk.
        check_disk(batch, reserve, 2 * len(remaining) * bytes_per_frame)
        await_gpu(batch, status, row, shot["device"]["backend"])
        request = list(render_requests(shot, files, captured, remaining, dimensions, stage))
        report = run_pipeline(batch, status, row, request, config, len(accepted))
        kept, errors = good_outputs(report, remaining, directory, dimensions, batch / "j", suffix)
        failures.extend(errors)
        accepted.update(kept)
        publish_frame_index(frame_manifest, captured, dimensions, desired, accepted, failures)
        if (batch / "cancel.request").is_file():
            row["completed_frames"] = len(accepted)
            raise Paused()
        if any(disk_full(error.get("message", "")) for error in errors):
            row["completed_frames"] = len(accepted)
            check_disk(batch, reserve, 2 * (len(desired) - len(accepted)) * bytes_per_frame, ran_out=True)
    missing = publish_frame_index(frame_manifest, captured, dimensions, desired, accepted, failures)
    if missing:
        reason = failures[-1].get("message") if failures else "帧验证未通过"
        raise Failure("VALIDATION_FAILED", f"{stage}: {len(missing)} 帧待处理。{reason}")
    row["completed_frames"] = len(accepted)
    row["state"] = stage + "_encoding"
    row["message"] = "正在编码并独立检查"
    write_status(batch, status)

    if str(CLI_DIR.parent) not in sys.path:
        sys.path.insert(0, str(CLI_DIR.parent))
    video = shot_output / (stage + ".mp4")
    frame_paths = [Path(accepted[frame].get('review',accepted[frame])['path']) for frame in desired]
    video_receipt = shot_output / (stage + "-video.json")
    if video.is_file() and video_receipt.is_file() and frame_manifest.is_file():
        metadata = read_json(video_receipt)
        if (metadata.get("sha256") == digest(video)
                and metadata.get("frame_hashes") == [accepted[frame]["sha256"] for frame in desired]):
            row[stage] = video.relative_to(batch).as_posix()
            write_status(batch, status)
            return frame_paths
    if video.exists():
        preserve_old(video)
    review_dimensions = tuple(size + size % 2 for size in dimensions)
    metadata = encode_review(batch, config, frame_paths, video, review_dimensions)
    metadata["frame_hashes"] = [accepted[frame]["sha256"] for frame in desired]
    atomic_json(video_receipt, metadata)
    row[stage] = video.relative_to(batch).as_posix()
    write_status(batch, status)
    return frame_paths


def encode_review(batch, config, frame_paths, video, dimensions):
    from render_batch_media import encode_sequence

    reserve = config["manifest"].get("min_free_mb", 4096)
    # Include a possible staging copy and encoded output. On NTFS staging
    # normally hard-links inputs, and a shorter recording needs less than this.
    required = sum(path.stat().st_size for path in frame_paths) + len(frame_paths) * frame_space(dimensions)
    check_disk(batch, reserve, required)
    for attempt in (1, 2):
        if (batch / "cancel.request").is_file():
            raise Paused()
        try:
            return encode_sequence(frame_paths, video, ffmpeg=config["ffmpeg"], ffprobe=config["ffprobe"],
                                   width=dimensions[0], height=dimensions[1], fps=frame_rate(config['manifest']['delivery']['fps']))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if disk_full(exc):
                check_disk(batch, reserve, required, ran_out=True)
            if attempt == 2:
                raise


def run_shot(batch, status, row, shot, config):
    shot_dir = batch / "shots" / ("s-" + shot["id"])
    row["state"] = "preflight"
    row["message"] = "正在核查冻结的场景"
    write_status(batch, status)
    frozen = read_json(shot_dir / "frozen-inputs.json")
    files, cache = frozen["files"], frozen["cache"]
    if digest(files["file"]) != files["expected_sha256"]:
        raise Failure("CONFLICT", "The frozen Blender snapshot has changed")
    for asset in files["resources"]:
        if digest(asset["file"]) != asset["expected_sha256"]:
            raise Failure("CONFLICT", "A dependency of the frozen snapshot has changed")
    if (shot_dir / "source-settings.json").is_file():
        captured = read_json(shot_dir / "source-settings.json")
        if captured["cache_manifest"]:
            files["cache_manifest"] = captured["cache_manifest"]
    else:
        captured = probe_source(files, cache, shot, shot_dir, config)
    row["engine"] = captured["source_settings"]["engine"]
    row["camera"] = shot["camera"]
    row["frames"] = [shot["frame_start"], shot["frame_end"]]
    width, height = shot['resolution']
    admission=memory_admission((width,height),len(captured.get('native_passes',[])),Path(files['file']).stat().st_size,
                               shot['device']['backend'],composite=shot['compositor_enabled'])
    row['memory_admission']=admission
    if not admission['admitted']:
        raise Failure('RESOURCE_LIMIT','Insufficient free working set or GPU memory for this shot estimate')
    shrink = min(1, 480 / max(width, height))
    preview_size = tuple(max(16, round(value * shrink / 2) * 2) for value in (width, height))
    render_phase(batch, status, row, shot, files, captured, "preview", preview_size, config)
    final_frames = render_phase(batch, status, row, shot, files, captured, "final", (width, height), config)
    index = read_json(shot_dir / 'final-frames.json')
    review = read_json(shot_dir / 'final-video.json')
    effective = captured['source_settings']['effective']
    atomic_json(shot_dir / 'handoff.json', {
        'schema_version':'1.0','shot_id':shot['id'],'scene':shot['scene'],'view_layer':shot['view_layer'],
        'camera':shot['camera'],'frame_start':shot['frame_start'],'frame_end':shot['frame_end'],
        'fps':config['manifest']['delivery']['fps'],'resolution':[width,height],
        'delivery_profile':shot['delivery_profile'],'output_passes':shot['output_passes'],
        'compositor_enabled':shot['compositor_enabled'],'scene_effects':captured.get('scene_effects', []),
        'memory_admission':admission,
        'color_space':'scene_linear' if shot['delivery_profile']=='MASTER_EXR32' else 'sRGB',
        'view_transform':effective['view_transform'],'view_settings':effective['color'],
        'alpha':'PREMUL' if shot['delivery_profile']=='MASTER_EXR32' else 'STRAIGHT',
        'source_sha256':files['expected_sha256'],'review':review,
        'frames':[{'frame':frame,**index['frames'][str(frame)]} for frame in range(shot['frame_start'],shot['frame_end']+1)],
    })
    row['handoff'] = (shot_dir/'handoff.json').relative_to(batch).as_posix()
    row["state"] = "completed"
    row["message"] = "已完成"
    write_status(batch, status)
    return final_frames


@contextmanager
def local_queue(batch, status, config):
    # Use one session-wide dispatch slot, plus the previous release's slot
    # for interoperability if it uses the same Blender installation.
    slot = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BlenderRenderBatch/.batch-executor-slot"
    legacy = Path(config["blender"]).parent / ".batch-executor-slot"
    while True:
        if (batch / "cancel.request").is_file():
            raise Paused("已取消队列等待，可从冻结输入继续")
        lock = PathLocks([slot, legacy])
        try:
            lock.__enter__()
        except Failure as exc:
            if exc.code != "CONFLICT":
                raise
            status["message"] = "工作站的批次执行器正忙，等待前一个批次"
            status["resource_wait"] = {"kind": "local_slot", "message": status["message"]}
            write_status(batch, status)
            time.sleep(.5)
        else:
            break
    try:
        status.pop("resource_wait", None)
        yield
    finally:
        lock.__exit__(None, None, None)


def await_gpu(batch, status, row, backend):
    if backend == "CPU":
        row["gpu_admission"] = {"state": "not_required"}
        return
    idle_samples = busy_samples = 0
    while True:
        if (batch / "cancel.request").is_file():
            raise Paused("已取消 GPU 等待，可从冻结输入继续")
        observation = gpu_sample(backend)
        row["gpu_admission"] = observation
        if observation["state"] == "unavailable":
            # Leave the decision visible: admission is best effort on Windows
            # drivers that expose neither monitor, not a claim the GPU is idle.
            row["gpu_admission"]["message"] = "无法读取 GPU 占用，按本机互斥队列执行"
            break
        if observation["state"] == "idle":
            idle_samples += 1
            if not busy_samples or idle_samples == 2:
                break
        else:
            busy_samples += 1
            idle_samples = 0
        message = f"观测到 GPU 繁忙，等待资源（镜头 {row['id']}）"
        status.update(state="queued", message=message,
                      resource_wait={"kind": "gpu", "message": message, "observation": observation})
        write_status(batch, status)
        time.sleep(1)
    status.pop("resource_wait", None)
    status.update(state="running", message="本机顺序执行")
    write_status(batch, status)


def execute_ready(batch, status, config):
    results = {}
    status.update(state="running", message="本机顺序执行")
    write_status(batch, status)
    for shot, row in zip(config["manifest"]["shots"], status["shots"]):
        if (batch / "cancel.request").is_file():
            raise Paused()
        frozen = batch / "shots" / ("s-" + shot["id"]) / "frozen-inputs.json"
        if row["state"] in ("failed", "paused") and not frozen.is_file():
            continue
        try:
            results[shot["id"]] = run_shot(batch, status, row, shot, config)
        except DiskPaused as pause:
            row.update(state="paused", message=str(pause))
            write_status(batch, status)
        except Paused as pause:
            row.update(state="paused", message=str(pause) or "已暂停，保留通过核验的帧")
            raise
        except (Failure, OSError, ValueError, RuntimeError, KeyError, ImportError,
                subprocess.SubprocessError) as exc:
            if disk_full(exc):
                row.update(state="paused", message="磁盘空间在写入时耗尽。已验帧保留，可在释放空间后继续。")
            else:
                row.update(state="failed", message=str(exc)[:5000])
            write_status(batch, status)
    all_good = all(row["state"] == "completed" for row in status["shots"])
    if results and len(status["shots"]) > 1:
        try:
            status["review"] = group_review(batch, config, results, all_good)
            status.pop("review_error", None)
        except (Failure, DiskPaused, OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
            status["review_error"] = str(exc)[:5000]
            all_good = False
    elif all_good:
        status["review"] = status["shots"][0]["final"]
    waiting_disk = any(row["state"] == "paused" for row in status["shots"])
    status["state"] = "completed" if all_good else "partial" if results else "paused" if waiting_disk else "failed"
    status["message"] = {"completed": "整组可审片", "partial": "部分完成", "failed": "批次失败",
                         "paused": "空间不足，已暂停"}[status["state"]]
    write_status(batch, status)
    return status


def execute(batch):
    config = read_json(batch / "launch.json")
    with PathLocks([batch]):
        status = read_json(batch / "status.json")
        if status["state"] != "queued":
            raise Failure("CONFLICT", "Only a newly queued batch can use this entry point")
        status["supervisor_pid"] = os.getpid()
        try:
            with local_queue(batch, status, config):
                return execute_ready(batch, status, config)
        except Paused as pause:
            status.pop("resource_wait", None)
            status.update(state="paused", message=str(pause) or "已暂停，可以从冻结输入继续")
            write_status(batch, status)
            return status


def group_review(batch, config, results, all_good):
    from render_batch_media import placeholder

    output = batch / ("review.mp4" if all_good else f"review-incomplete-{uuid.uuid4().hex[:8]}.mp4")
    frames = []
    slots = []
    for shot in config["manifest"]["shots"]:
        count = shot["frame_end"] - shot["frame_start"] + 1
        available = results.get(shot["id"])
        slots.append({"shot": shot["id"], "first": len(frames) + 1, "frames": count,
                      "state": "complete" if available else "placeholder"})
        if not available:
            card = batch / ("placeholder-" + shot["id"] + ".png")
            if not card.is_file():
                delivery=config['manifest']['delivery']
                placeholder(card, shot["id"], ffmpeg=config["ffmpeg"], width=delivery['width'], height=delivery['height'])
            available = [card] * count
        frames.extend(available)
    if output.exists():
        preserve_old(output)
    delivery=config['manifest']['delivery']
    metadata = encode_review(batch, config, frames, output, (delivery['width'],delivery['height']))
    atomic_json(output.with_suffix(".json"), {**metadata, "slots": slots, "complete": all_good})
    return output.name


def freeze_batch(batch, status, manifest):
    for shot, row in zip(manifest["shots"], status["shots"]):
        shot_dir = batch / "shots" / ("s-" + shot["id"])
        if row["state"] not in ("queued", "paused", "freezing") or (shot_dir / "frozen-inputs.json").is_file():
            continue
        shot_dir.mkdir(parents=True, exist_ok=True)
        row["state"] = "freezing"
        row["message"] = "正在复制和验证输入"
        write_status(batch, status)
        try:
            files, cache = freeze_inputs(shot, shot_dir, batch, manifest.get("min_free_mb", 4096))
            atomic_json(shot_dir / "frozen-inputs.json", {"files": files, "cache": cache})
            row["state"] = "queued"
            row["message"] = "输入已冻结"
        except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
            if disk_full(exc):
                row.update(state="paused", message="冻结元数据写入时磁盘耗尽。已验证的输入副本已保留。")
            else:
                row.update(state="failed", message=str(exc)[:5000])
        except Paused as pause:
            row["state"] = "paused"
            row["message"] = str(pause)
    any_queued = any(row["state"] == "queued" for row in status["shots"])
    any_paused = any(row["state"] == "paused" for row in status["shots"])
    status["state"] = "queued" if any_queued else "paused" if any_paused else "failed"
    status["message"] = {"queued": "输入已冻结，等待调度", "paused": "空间不足，已暂停",
                         "failed": "输入预检失败"}[status["state"]]
    write_status(batch, status)


def submit(args, directory):
    require(os.name == "nt", "Local batch worker currently uses Windows file guards")
    manifest = normalize_batch(read_json(args.manifest))
    require(30 <= args.timeout <= 86400, "Use a pipeline timeout from 30 to 86400 seconds")
    config = {"manifest": manifest, "blender": str(absolute_file(args.blender.resolve())),
              "ffmpeg": str(absolute_file(args.ffmpeg)), "ffprobe": str(absolute_file(args.ffprobe)),
              "transactions_dir": str(args.transactions_dir.resolve()), "timeout": args.timeout, "runtime_release": VERSION}
    directory.mkdir(parents=True, exist_ok=True)
    if str(CLI_DIR.parent) not in sys.path:
        sys.path.insert(0, str(CLI_DIR.parent))
    from render_batch_media import preflight_tools
    try:
        preflight_tools(config["ffmpeg"], config["ffprobe"], directory)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise Failure("UNSUPPORTED", str(exc)) from exc
    batch = directory / uuid.uuid4().hex[:16]
    batch.mkdir()
    status = {"version": "1.0", "runtime_release": VERSION, "batch_id": batch.name, "state": "freezing", "message": "批次已接收", "review": None,
              "shots": [{"id": shot["id"], "state": "queued", "message": "等待",
                         "completed_frames": 0, "total_frames": shot["frame_end"] - shot["frame_start"] + 1,
                         "preview": None, "final": None, "attempts": []} for shot in manifest["shots"]]}
    with PathLocks([batch]):
        atomic_json(batch / "launch.json", config)
        freeze_batch(batch, status, manifest)
    if status["state"] in ("failed", "paused"):
        return batch, status
    return invoke(batch, status, args.background)


def invoke(batch, status, background):
    if background:
        with (batch / "supervisor.log").open("ab") as log:
            subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), str(batch)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        return batch, status
    return batch, execute(batch)


def resume(batch, background):
    with PathLocks([batch]):
        status = read_json(batch / "status.json")
        delayed = status["state"] in {"queued", "freezing"} and supervisor_quiet(status)
        require(status["state"] in ("paused", "partial", "failed", "running") or delayed,
                "Batch must be paused or incomplete before resuming")
        needs_freeze = any(shot["state"] in {"queued", "paused", "freezing"} and
                           not (batch / "shots" / ("s-" + shot["id"]) / "frozen-inputs.json").is_file()
                           for shot in status["shots"])
        if needs_freeze:
            freeze_batch(batch, status, read_json(batch / "launch.json")["manifest"])
        any_frozen = any((batch / "shots" / ("s-" + shot["id"]) / "frozen-inputs.json").is_file()
                         for shot in status["shots"])
        if not any_frozen and status["state"] == "paused":
            return batch, status
        require(any_frozen, "No intact frozen shot exists to resume")
        marker = batch / "cancel.request"
        if marker.exists():
            preserve_old(marker)
        status.pop("resource_wait", None)
        status.pop("supervisor_pid", None)
        status.update(state="queued", message="从冻结输入继续",
                      resume_count=status.get("resume_count", 0) + 1)
        write_status(batch, status)
    return invoke(batch, status, background)


def supervisor_quiet(status):
    return time.time() - status.get("updated_unix", 0) > SUPERVISOR_GRACE


def status_snapshot(batch):
    status = read_json(batch / "status.json")
    if status["state"] in {"queued", "freezing", "running"} and supervisor_quiet(status):
        try:
            with PathLocks([batch]):
                status = read_json(batch / "status.json")
                if status["state"] in {"queued", "freezing", "running"} and supervisor_quiet(status):
                    status["health"] = "resume_available_after_supervisor_exit"
        except Failure as exc:
            if exc.code != "CONFLICT":
                raise
    return status


def dispatch(args):
    require(not args.compact and not args.full, "Batch status already has a bounded public projection")
    directory = args.jobs_dir.resolve() / "b"
    if args.batch_action == "submit":
        batch, status = submit(args, directory)
    else:
        require(BATCH_ID.fullmatch(args.batch_id), "Invalid batch id")
        batch = directory / args.batch_id
        if args.batch_action == "resume":
            batch, status = resume(batch, args.background)
        elif args.batch_action == "cancel":
            status = read_json(batch / "status.json")
            require(not args.background and status["state"] in ("queued", "running"),
                    "Cancellation requires a queued or running batch")
            atomic_json(batch / "cancel.request", {"requested_unix": time.time()})
            status["cancellation_requested"] = True
        else:
            require(not args.background, "Batch status does not start a job")
            seconds = getattr(args, "wait_seconds", 0)
            require(0 <= seconds <= 55, "Batch wait must be 0–55 seconds")
            end = time.monotonic() + seconds
            while True:
                status = status_snapshot(batch)
                if status["state"] in TERMINAL or status.get("health") or time.monotonic() >= end:
                    break
                time.sleep(min(.5, max(0, end - time.monotonic())))
    return envelope(f"render.batch.{args.batch_action}", batch.name, status,
                    artifacts={"batch_directory": str(batch), "status": str(batch / "status.json")})


if __name__ == "__main__":
    execute(Path(sys.argv[1]).resolve())

