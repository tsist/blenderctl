"""CLI execution orchestrator for describe, run, sweep, and batch workflows."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from . import deployment
from . import protocol


def _short_bake_dir(jobs_root: Path, job_id: str) -> Path:
    short = hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:10]
    d = jobs_root / "bakes" / short
    d.mkdir(parents=True, exist_ok=True)
    return d


def describe() -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    blender_bin = deployment.default_blender_binary(require_exists=False)
    return protocol.CommandResponse(
        ok=True,
        command="describe",
        job_id=protocol.new_job_id("desc"),
        data={
            **contract.describe_contract(),
            "blender_binary": str(blender_bin),
            "addon_directory": str(deployment.addon_directory()),
        },
    )


def _run_blender_script(
    script_path: Path,
    arg_paths: list[Path],
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess:
    blender_bin = str(deployment.default_blender_binary())
    cmd = [
        blender_bin,
        "--background",
        "--factory-startup",
        "--python",
        str(script_path),
        "--",
        *[str(p) for p in arg_paths],
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )


def run_single(
    raw_request: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
) -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    job_id = protocol.new_job_id("run")
    job_dir = jobs_dir.resolve() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Validate before launching Blender
    try:
        normalized = contract.normalize_request(raw_request)
    except contract.PhysicsContractError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error={"code": exc.code, "message": exc.message},
        )

    input_blend_str = raw_request.get("input_blend")
    if not input_blend_str:
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error={"code": "VALIDATION_ERROR", "message": "input_blend path is required"},
        )
    input_blend = Path(input_blend_str).resolve()
    if not input_blend.is_file():
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error={"code": "SCENE_STATE_ERROR", "message": f"input_blend not found: {input_blend}"},
        )

    input_sha256 = protocol.sha256_file(input_blend)
    output_blend = Path(raw_request["output_blend"]).resolve() if raw_request.get("output_blend") else (job_dir / "output.blend")

    worker_req = copy.deepcopy(raw_request)
    if worker_req.get("bake") and not worker_req.get("bake_directory"):
        worker_req["bake_directory"] = str(_short_bake_dir(jobs_dir.resolve(), job_id))

    req_file = job_dir / "worker_request.json"
    res_file = job_dir / "worker_result.json"
    worker_payload = {
        "tools_dir": str(deployment.addon_directory().parent),
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "request": worker_req,
    }
    req_file.write_text(json.dumps(worker_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    worker_script = Path(__file__).resolve().parent / "worker.py"
    try:
        proc = _run_blender_script(worker_script, [req_file, res_file], timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired:
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error={"code": "TIMEOUT", "message": f"Blender worker timed out after {timeout_seconds}s"},
        )

    (job_dir / "worker_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (job_dir / "worker_stderr.log").write_text(proc.stderr, encoding="utf-8")

    if not res_file.is_file():
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error={
                "code": "EXECUTION_ERROR",
                "message": f"Worker exited ({proc.returncode}) without result file",
                "stderr_tail": proc.stderr[-1500:],
            },
        )

    worker_res = json.loads(res_file.read_text(encoding="utf-8"))
    if not worker_res.get("ok"):
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error=worker_res.get("error", {"code": "EXECUTION_ERROR", "message": "Worker failed"}),
            data=worker_res,
        )

    artifacts = [str(output_blend)]
    if "bake" in worker_res and worker_res["bake"].get("bake_directory"):
        artifacts.append(worker_res["bake"]["bake_directory"])
    if "export" in worker_res and worker_res["export"].get("export_path"):
        artifacts.append(worker_res["export"]["export_path"])

    cold_verify_data = None
    if normalized.get("verify_cold_reopen"):
        p = normalized["parameters"]
        end_frame_key = str(p["frame_end"])
        expected_end = worker_res.get("inspection", {}).get("frames", {}).get(end_frame_key)
        verify_spec = {
            "output_blend": str(output_blend),
            "target_object": worker_res.get("target_object", normalized["target_object"]),
            "frame_start": p["frame_start"],
            "frame_end": p["frame_end"],
            "expected_end_summary": expected_end,
        }
        v_spec_file = job_dir / "verify_spec.json"
        v_res_file = job_dir / "verify_result.json"
        v_spec_file.write_text(json.dumps(verify_spec, indent=2), encoding="utf-8")
        v_script = Path(__file__).resolve().parent / "physics_verifier.py"
        v_proc = _run_blender_script(v_script, [v_spec_file, v_res_file], timeout_seconds=timeout_seconds)
        if not v_res_file.is_file():
            return protocol.CommandResponse(
                ok=False,
                command="run",
                job_id=job_id,
                error={
                    "code": "VERIFICATION_FAILED",
                    "message": f"Cold verifier crashed ({v_proc.returncode})",
                    "stderr_tail": v_proc.stderr[-1000:],
                },
            )
        cold_verify_data = json.loads(v_res_file.read_text(encoding="utf-8"))
        if not cold_verify_data.get("passed"):
            return protocol.CommandResponse(
                ok=False,
                command="run",
                job_id=job_id,
                error={"code": "VERIFICATION_FAILED", "message": "Cold reopen verification failed"},
                data={"worker": worker_res, "cold_reopen_verify": cold_verify_data},
            )

    output_sha256 = protocol.sha256_file(output_blend) if output_blend.is_file() else ""
    summary = {
        "input_blend": str(input_blend),
        "input_sha256": input_sha256,
        "output_blend": str(output_blend),
        "output_sha256": output_sha256,
        "worker": worker_res,
        "cold_reopen_verify": cold_verify_data,
        "job_dir": str(job_dir),
    }
    (job_dir / "receipt.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return protocol.CommandResponse(
        ok=True,
        command="run",
        job_id=job_id,
        data=summary,
        artifacts=artifacts,
    )


def run_sweep(
    raw_sweep: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
) -> protocol.CommandResponse:
    sweep_id = protocol.new_job_id("sweep")
    sweep_dir = jobs_dir.resolve() / sweep_id
    sweep_dir.mkdir(parents=True, exist_ok=True)

    base_req = copy.deepcopy(raw_sweep.get("base_request") or {})
    variants = raw_sweep.get("variants") or []
    if not isinstance(variants, list) or not variants:
        return protocol.CommandResponse(
            ok=False,
            command="sweep",
            job_id=sweep_id,
            error={"code": "VALIDATION_ERROR", "message": "sweep requires non-empty 'variants' list"},
        )

    runs = []
    all_ok = True
    for idx, var in enumerate(variants):
        req = copy.deepcopy(base_req)
        var_params = var.get("parameters") or {}
        req.setdefault("parameters", {}).update(var_params)
        for k in ("preset_id", "bridge_mode", "bake", "verify_cold_reopen", "collider_object", "instance_collection"):
            if k in var:
                req[k] = var[k]
        req["output_blend"] = str(sweep_dir / f"variant_{idx:02d}" / "output.blend")
        res = run_single(req, sweep_dir, timeout_seconds=timeout_seconds)
        if not res.ok:
            all_ok = False
        insp = res.data.get("worker", {}).get("inspection", {}) if res.ok else {}
        runs.append(
            {
                "index": idx,
                "label": var.get("label", f"variant_{idx}"),
                "ok": res.ok,
                "job_id": res.job_id,
                "parameters_override": var_params,
                "centroid_displacement": insp.get("centroid_displacement"),
                "late_frame_jitter": insp.get("late_frame_jitter"),
                "resting_ratio": insp.get("resting_ratio"),
                "output_blend": req["output_blend"],
                "error": res.error,
            }
        )

    report = {"sweep_id": sweep_id, "total": len(runs), "passed": sum(1 for r in runs if r["ok"]), "runs": runs}
    report_path = sweep_dir / "sweep_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return protocol.CommandResponse(
        ok=all_ok,
        command="sweep",
        job_id=sweep_id,
        data=report,
        artifacts=[str(report_path)],
        error=None if all_ok else {"code": "SWEEP_PARTIAL_FAILURE", "message": "One or more sweep variants failed"},
    )


def run_batch(
    raw_batch: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
) -> protocol.CommandResponse:
    batch_id = protocol.new_job_id("batch")
    batch_dir = jobs_dir.resolve() / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    items = raw_batch.get("items") or []
    if not isinstance(items, list) or not items:
        return protocol.CommandResponse(
            ok=False,
            command="batch",
            job_id=batch_id,
            error={"code": "VALIDATION_ERROR", "message": "batch requires non-empty 'items' list"},
        )

    results = []
    all_ok = True
    for idx, item in enumerate(items):
        res = run_single(item, batch_dir, timeout_seconds=timeout_seconds)
        if not res.ok:
            all_ok = False
        results.append(
            {
                "index": idx,
                "ok": res.ok,
                "job_id": res.job_id,
                "target_object": item.get("target_object"),
                "preset_id": item.get("preset_id"),
                "error": res.error,
                "output_blend": res.data.get("output_blend") if res.ok else None,
            }
        )

    report = {"batch_id": batch_id, "total": len(results), "passed": sum(1 for r in results if r["ok"]), "items": results}
    report_path = batch_dir / "batch_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return protocol.CommandResponse(
        ok=all_ok,
        command="batch",
        job_id=batch_id,
        data=report,
        artifacts=[str(report_path)],
        error=None if all_ok else {"code": "BATCH_PARTIAL_FAILURE", "message": "One or more batch items failed"},
    )
