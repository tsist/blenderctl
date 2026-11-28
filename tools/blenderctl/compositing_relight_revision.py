"""Dependency-free revision publication contract; host serializes API calls.

This module owns logical publication, not IPC, workers, GPU or Blender UI.
Never use a worker's artifact directly: only a Decision(published=True) qualifies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty exact str")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("revision must be an exact positive int (bool rejected)")
    return value


def _json_tree(value: Any, ancestors: set[int]) -> None:
    """Accept only exact JSON builtins, finite numbers and acyclic containers."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return
    if type(value) not in (dict, list):
        raise ValueError("parameters must contain only exact JSON builtin types")
    ident = id(value)
    if ident in ancestors:
        raise ValueError("cyclic parameters")
    ancestors.add(ident)
    try:
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be exact strings")
                _json_tree(child, ancestors)
        else:
            for child in value:
                _json_tree(child, ancestors)
    finally:
        ancestors.remove(ident)


@dataclass(frozen=True)
class Snapshot:
    """Deeply isolated immutable canonical bytes. Decoding returns a fresh copy."""
    canonical: bytes
    sha256: str

    @classmethod
    def capture(cls, parameters: dict) -> Snapshot:
        if type(parameters) is not dict:
            raise ValueError("parameters must be an exact dict")
        _json_tree(parameters, set())
        detached = copy.deepcopy(parameters)
        canonical = json.dumps(detached, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        return cls(canonical, hashlib.sha256(canonical).hexdigest())

    def decode(self) -> dict:
        return json.loads(self.canonical)


@dataclass(frozen=True)
class Request:
    session_id: str
    incarnation_id: str
    cache_id: str
    revision: int
    parameters: Snapshot
    request_id: str
    reason: str

    def envelope(self) -> dict:
        """A fresh transport envelope; its mutations cannot alter the request."""
        return {"session_id": self.session_id, "incarnation_id": self.incarnation_id,
                "cache_id": self.cache_id, "revision": self.revision,
                "parameters_sha256": self.parameters.sha256,
                "request_id": self.request_id, "parameters": self.parameters.decode(),
                "reason": self.reason}


@dataclass(frozen=True)
class Published:
    request: Request
    artifact: Snapshot


@dataclass(frozen=True)
class Decision:
    published: bool
    reason: str
    next_dispatch: Request | None = None


class RevisionGate:
    """One logical active + one latest pending; use on one owner/event thread.

    The host must dispatch only returned submit() / next_dispatch requests.
    Calling cancel_active() does not mean the physical worker has stopped.
    close()/reopen() fences old output; the host separately reaps old workers.
    Session/cache identities should represent an owned session and immutable
    cache generation/content, not merely a path that can be overwritten.
    """
    _FIELDS = {"session_id", "incarnation_id", "cache_id", "revision",
               "parameters_sha256", "request_id", "status", "artifact"}
    _STATUSES = {"passed", "failed", "cancelled", "crashed"}

    def __init__(self, session_id: str, cache_id: str):
        self._session_id = _text(session_id, "session_id")
        self._cache_id = _text(cache_id, "cache_id")
        self._incarnation_id = uuid4().hex
        self._seen_sessions = {self._session_id}
        self._high_water = 0
        self._active: Request | None = None
        self._pending: Request | None = None
        self._desired: Request | None = None
        self._cancel_requested = False
        self._last_passed: Published | None = None
        self._closed = False

    @property
    def active(self) -> Request | None:
        return self._active

    @property
    def pending(self) -> Request | None:
        return self._pending

    @property
    def desired(self) -> Request | None:
        return self._desired

    @property
    def last_passed(self) -> Published | None:
        return self._last_passed

    @property
    def high_water(self) -> int:
        return self._high_water

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_passed_is_current(self) -> bool:
        return (not self._closed and self._last_passed is not None
                and self._last_passed.request == self._desired)

    def submit(self, revision: int, parameters: dict, *, reason: str = "edit") -> Request | None:
        """Return a dispatch only if idle; otherwise replace latest pending.

        All validations occur before mutation. ESC and Undo require a new
        revision even when their parameters equal a previously passed request.
        """
        if self._closed:
            raise RuntimeError("session is closed")
        revision = _revision(revision)
        if revision <= self._high_water:
            raise ValueError("revision must exceed lifetime high_water")
        reason = _text(reason, "reason")
        snapshot = Snapshot.capture(parameters)
        request = Request(self._session_id, self._incarnation_id, self._cache_id,
                          revision, snapshot, uuid4().hex, reason)
        self._high_water = revision
        self._desired = request
        if self._active is None:
            self._active = request
            self._cancel_requested = False
            return request
        self._pending = request
        return None

    def cancel_active(self) -> Request | None:
        """Invalidate publication, retaining occupancy until terminal ack/reap."""
        if self._active is not None:
            self._cancel_requested = True
        return self._active

    def cancel_pending(self) -> Request | None:
        previous = self._pending
        self._pending = None
        # Do not roll desired back: older active must not become current again.
        return previous

    def _advance(self) -> Request | None:
        self._active = self._pending
        self._pending = None
        self._cancel_requested = False
        return self._active

    def complete(self, message: dict) -> Decision:
        """Validate untrusted completion before changing any state.

        Exact envelope identity is necessary even for failed/cancelled messages;
        malformed/alien output must not drain the current active request.
        'passed' is a host assertion that external image/consumer checks passed,
        not an assertion this state machine independently verifies pixels.
        """
        if type(message) is not dict or set(message) != self._FIELDS:
            return Decision(False, "invalid_schema")
        try:
            _revision(message["revision"])
            for name in ("session_id", "incarnation_id", "cache_id", "request_id"):
                _text(message[name], name)
            sha = message["parameters_sha256"]
            if type(sha) is not str or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
                raise ValueError("invalid SHA")
            if type(message["status"]) is not str or message["status"] not in self._STATUSES:
                raise ValueError("invalid status")
            artifact = Snapshot.capture(message["artifact"])
        except (ValueError, RecursionError, OverflowError):
            return Decision(False, "invalid_types")
        if self._closed:
            return Decision(False, "closed_session")
        active = self._active
        if active is None:
            return Decision(False, "no_active")
        expected = (active.session_id, active.incarnation_id, active.cache_id,
                    active.revision, active.parameters.sha256, active.request_id)
        received = (message["session_id"], message["incarnation_id"], message["cache_id"],
                    message["revision"], sha, message["request_id"])
        if received != expected:
            return Decision(False, "identity_mismatch")
        if self._cancel_requested:
            reason = "cancel_requested"
        elif active != self._desired:
            reason = "superseded"
        elif message["status"] != "passed":
            reason = message["status"]
        else:
            self._last_passed = Published(active, artifact)
            reason = "published"
        return Decision(reason == "published", reason, self._advance())

    def worker_exited(self, request_id: str) -> Decision:
        """Use only after the host positively observes this owned worker exit."""
        request_id = _text(request_id, "request_id")
        if self._closed or self._active is None or self._active.request_id != request_id:
            return Decision(False, "unknown_worker")
        active = self._active
        message = completion(active, "crashed")
        return self.complete(message)

    def close(self) -> Request | None:
        """Fence all callbacks. Return previous active for host cancellation/reap."""
        previous = self._active
        self._closed = True
        self._active = self._pending = self._desired = None
        self._last_passed = None
        self._cancel_requested = False
        return previous

    def reopen(self, session_id: str, *, cache_id: str | None = None) -> None:
        """Require a never-used session id; keep lifetime revision high_water."""
        if not self._closed:
            raise RuntimeError("close the old owned session before reopening")
        new_session = _text(session_id, "session_id")
        if new_session in self._seen_sessions:
            raise ValueError("reopen requires a never-used session_id")
        new_cache = self._cache_id if cache_id is None else _text(cache_id, "cache_id")
        self._session_id = new_session
        self._seen_sessions.add(new_session)
        self._cache_id = new_cache
        self._incarnation_id = uuid4().hex
        self._closed = False


def completion(request: Request, status: str = "passed", artifact: dict | None = None) -> dict:
    """Convenience fake/host completion builder, not a real IPC implementation."""
    return {"session_id": request.session_id, "incarnation_id": request.incarnation_id,
            "cache_id": request.cache_id, "revision": request.revision,
            "parameters_sha256": request.parameters.sha256, "request_id": request.request_id,
            "status": status, "artifact": {} if artifact is None else artifact}
