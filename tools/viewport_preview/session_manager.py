# SPDX-License-Identifier: GPL-3.0-or-later
"""Hot isolated-desktop session manager (start / status / stop / list / submit)."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import desktop_runner


DEFAULT_SESSIONS_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "blenderctl" / "preview-sessions"
WORKER_SCRIPT = THIS_DIR / "viewport_worker.py"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


def _terminate_pid(pid: int) -> bool:
    if pid <= 0 or os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = kernel32.OpenProcess(0x0001 | 0x00100000, False, int(pid))
    if not h:
        return False
    try:
        kernel32.TerminateProcess(h, 15)
        kernel32.WaitForSingleObject(h, 5000)
        return True
    finally:
        kernel32.CloseHandle(h)


def _supervisor_main(session_dir: Path) -> None:
    """Tiny detached supervisor holding the Win32 HDESK handle until Blender exits."""
    if os.name != "nt":
        raise RuntimeError("Hot isolated-desktop sessions require Windows")
    from ctypes import wintypes

    meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8-sig"))
    blender = meta["blender"]
    idle_timeout = float(meta["idle_timeout"])
    cell_size = int(meta.get("window_size", 640))
    desk_name = meta["desktop"]
    log_path = str(session_dir / "blender.log")

    argv = [
        blender,
        "--factory-startup",
        "-noaudio",
        "-p",
        "0",
        "0",
        str(cell_size),
        str(cell_size),
        "--python",
        str(WORKER_SCRIPT),
        "--",
        "--session-dir",
        str(session_dir),
        "--idle-timeout",
        str(idle_timeout),
    ]

    pid, h_proc, h_desk = desktop_runner.spawn_detached_desktop_host(
        argv, desktop_name=desk_name, log_path=log_path
    )
    meta["blender_pid"] = pid
    meta["supervisor_pid"] = os.getpid()
    meta["state"] = "running"
    _atomic_json(session_dir / "session.json", meta)

    user32, kernel32 = desktop_runner._win_apis()
    try:
        kernel32.WaitForSingleObject(wintypes.HANDLE(h_proc), 0xFFFFFFFF)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_proc))
        user32.CloseDesktop(wintypes.HANDLE(h_desk))
        meta["state"] = "stopped"
        meta["stopped_at"] = time.time()
        _atomic_json(session_dir / "session.json", meta)


def start_session(
    blender: str,
    *,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    idle_timeout: float = 180.0,
    window_size: int = 640,
    preload_file: str | None = None,
    startup_timeout: float = 25.0,
) -> dict[str, Any]:
    """Launch a new isolated-desktop hot preview session and wait until ready."""
    sessions_root.mkdir(parents=True, exist_ok=True)
    session_id = f"vp-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    (session_dir / "requests").mkdir()
    (session_dir / "responses").mkdir()

    desk_name = f"BlenderPreviewHot_{session_id}"
    meta = {
        "session_id": session_id,
        "directory": str(session_dir),
        "blender": str(Path(blender).resolve()),
        "desktop": desk_name,
        "idle_timeout": float(idle_timeout),
        "window_size": int(window_size),
        "created_at": time.time(),
        "state": "starting",
    }
    _atomic_json(session_dir / "session.json", meta)

    # Spawn detached supervisor using the same Python interpreter
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--supervise", str(session_dir)],
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for daemon_status.json == ready
    deadline = time.monotonic() + startup_timeout
    status_file = session_dir / "daemon_status.json"
    while time.monotonic() < deadline:
        if status_file.is_file():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8-sig"))
                if st.get("phase") == "ready":
                    cur_meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8-sig"))
                    out = {**cur_meta, "daemon_status": st}
                    if preload_file:
                        # Warm-load is deferred to first capture or done via no-op; record hint
                        out["preload_hint"] = str(Path(preload_file).resolve())
                    return out
            except Exception:
                pass
        time.sleep(0.1)

    stop_session(session_id, sessions_root=sessions_root)
    raise TimeoutError(f"Hot preview session {session_id} did not become ready within {startup_timeout}s")


def get_session_status(session_id: str, *, sessions_root: Path = DEFAULT_SESSIONS_ROOT) -> dict[str, Any]:
    session_dir = sessions_root / session_id
    meta_file = session_dir / "session.json"
    if not meta_file.is_file():
        raise FileNotFoundError(f"Unknown preview session: {session_id}")
    meta = json.loads(meta_file.read_text(encoding="utf-8-sig"))
    b_pid = int(meta.get("blender_pid") or 0)
    alive = _pid_alive(b_pid)
    status_file = session_dir / "daemon_status.json"
    daemon_status = None
    if status_file.is_file():
        try:
            daemon_status = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {
        **meta,
        "alive": alive,
        "daemon_status": daemon_status,
    }


def list_sessions(*, sessions_root: Path = DEFAULT_SESSIONS_ROOT) -> list[dict[str, Any]]:
    if not sessions_root.is_dir():
        return []
    rows = []
    for entry in sorted(sessions_root.iterdir()):
        if entry.is_dir() and (entry / "session.json").is_file():
            try:
                rows.append(get_session_status(entry.name, sessions_root=sessions_root))
            except Exception:
                pass
    return rows


def stop_session(session_id: str, *, sessions_root: Path = DEFAULT_SESSIONS_ROOT, timeout: float = 8.0) -> dict[str, Any]:
    session_dir = sessions_root / session_id
    meta_file = session_dir / "session.json"
    if not meta_file.is_file():
        raise FileNotFoundError(f"Unknown preview session: {session_id}")
    meta = json.loads(meta_file.read_text(encoding="utf-8-sig"))
    b_pid = int(meta.get("blender_pid") or 0)
    s_pid = int(meta.get("supervisor_pid") or 0)

    if _pid_alive(b_pid):
        (session_dir / "stop.request").write_text("stop", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(b_pid):
            time.sleep(0.1)
        if _pid_alive(b_pid):
            _terminate_pid(b_pid)

    # Give supervisor a moment to close desktop handle cleanly
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_alive(s_pid):
        time.sleep(0.05)

    return get_session_status(session_id, sessions_root=sessions_root)


def submit_to_session(
    session_id: str,
    spec: dict[str, Any],
    *,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    timeout: float = 90.0,
) -> dict[str, Any]:
    st = get_session_status(session_id, sessions_root=sessions_root)
    if not st["alive"]:
        raise RuntimeError(f"Preview session {session_id} is not running (state={st.get('state')})")

    session_dir = Path(st["directory"])
    req_id = f"req-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    req_file = session_dir / "requests" / f"{req_id}.json"
    res_file = session_dir / "responses" / f"{req_id}.json"

    _atomic_json(req_file, spec)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if res_file.is_file():
            try:
                payload = json.loads(res_file.read_text(encoding="utf-8-sig"))
                try:
                    res_file.unlink()
                except OSError:
                    pass
                return payload
            except Exception:
                pass
        if not _pid_alive(int(st.get("blender_pid") or 0)):
            raise RuntimeError(f"Preview session {session_id} exited unexpectedly while processing {req_id}")
        time.sleep(0.08)
    raise TimeoutError(f"Preview session {session_id} timed out after {timeout}s on request {req_id}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--supervise":
        _supervisor_main(Path(sys.argv[2]))
