"""Structured CLI protocol envelope, error codes, and deterministic job IDs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid
from typing import Any


EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_EXECUTION = 3
EXIT_VERIFICATION = 4


@dataclass
class CommandResponse:
    ok: bool
    command: str
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "command": self.command,
            "job_id": self.job_id,
            "data": self.data,
            "artifacts": self.artifacts,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


def new_job_id(prefix: str = "phys") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:8]
    return f"{prefix}-{stamp}-{short}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
