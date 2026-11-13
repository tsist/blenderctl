"""Public Python helper wrapper for programmatic invocation of physics_workflow_cli_v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import protocol, runner


def describe_workflow() -> dict[str, Any]:
    return runner.describe().to_dict()


def run_workflow(request: dict[str, Any], jobs_dir: str | Path, timeout_seconds: int = 300) -> dict[str, Any]:
    return runner.run_single(request, Path(jobs_dir), timeout_seconds=timeout_seconds).to_dict()


def sweep_workflow(request: dict[str, Any], jobs_dir: str | Path, timeout_seconds: int = 300) -> dict[str, Any]:
    return runner.run_sweep(request, Path(jobs_dir), timeout_seconds=timeout_seconds).to_dict()


def batch_workflow(request: dict[str, Any], jobs_dir: str | Path, timeout_seconds: int = 300) -> dict[str, Any]:
    return runner.run_batch(request, Path(jobs_dir), timeout_seconds=timeout_seconds).to_dict()
