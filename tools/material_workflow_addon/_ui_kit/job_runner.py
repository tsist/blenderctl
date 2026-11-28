# SPDX-License-Identifier: GPL-3.0-or-later
"""Non-blocking background subprocess runner with Blender 5.3 timer polling & HUD sync."""

from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, BinaryIO, Callable, Dict, Optional, Sequence

from .hud_overlay import HUDManager
from .theme_tokens import get_theme


class BackgroundJobRunner:
    """Runs heavy jobs asynchronously via subprocess + bpy.app.timers without freezing UI."""

    def __init__(self, domain: str, poll_interval: float = 0.25):
        self.domain = domain
        self.poll_interval = float(poll_interval)
        self._proc: Optional[subprocess.Popen] = None
        self._logs: tuple[BinaryIO, ...] = ()
        self._status_file: Optional[Path] = None
        self._result_file: Optional[Path] = None
        self._on_complete: Optional[Callable[[bool, Dict[str, Any]], None]] = None
        self._timer_fn: Optional[Callable[[], Optional[float]]] = None
        self._load_pre_fn: Optional[Callable[..., None]] = None
        self._started_at: float = 0.0
        self.label: str = ""
        self.detail: str = ""
        self.progress: float = 0.0
        self.running: bool = False

    def start(
        self,
        argv: Sequence[str],
        *,
        label: str,
        status_file: Optional[Path] = None,
        result_file: Optional[Path] = None,
        on_complete: Optional[Callable[[bool, Dict[str, Any]], None]] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        import bpy

        if self.running:
            raise RuntimeError("当前已有后台任务正在执行，请等待完成或先取消")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Polling without draining a pipe can leave the worker blocked on its output.
        with ExitStack() as pending:
            stdout = pending.enter_context(tempfile.TemporaryFile())
            stderr = pending.enter_context(tempfile.TemporaryFile())
            self._proc = subprocess.Popen(
                [str(x) for x in argv], stdout=stdout, stderr=stderr,
                cwd=str(cwd) if cwd else None, creationflags=creationflags,
            )
            self._logs = stdout, stderr
            pending.pop_all()
        self._status_file = Path(status_file) if status_file else None
        self._result_file = Path(result_file) if result_file else None
        self._on_complete = on_complete
        self._started_at = time.monotonic()
        self.label = label
        self.detail = "正在初始化后台工作进程…"
        self.progress = 0.08
        self.running = True

        theme = get_theme(self.domain)
        HUDManager.show(
            self.domain,
            f"{theme.tab_label} · {self.label}",
            self.detail,
            progress=self.progress,
            kind="INFO",
        )
        self._redraw()

        def _poll() -> Optional[float]:
            return self._tick()

        def _cancel_before_load(*_args) -> None:
            # Blender removes nonpersistent timers when it loads another file.
            # Finish cleanup first so the old job cannot write into the new scene.
            if self._load_pre_fn is _cancel_before_load:
                # Blender is iterating load_pre and will clear this callback itself.
                self._load_pre_fn = None
                self.cancel()

        self._timer_fn = _poll
        self._load_pre_fn = _cancel_before_load
        bpy.app.handlers.load_pre.append(_cancel_before_load)
        try:
            bpy.app.timers.register(_poll, first_interval=self.poll_interval)
        except Exception:
            self.cancel()
            raise

    def _unwatch(self, *, timer_running: bool = False) -> None:
        import bpy

        if self._load_pre_fn in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(self._load_pre_fn)
        self._load_pre_fn = None
        timer, self._timer_fn = self._timer_fn, None
        if timer is not None and not timer_running and bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)

    def _read_json(self, path: Optional[Path]) -> Optional[Dict[str, Any]]:
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _tail(stream: BinaryIO) -> str:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 6000))
        return stream.read().decode("utf-8", errors="replace")[-1500:]

    def _close_logs(self) -> None:
        for stream in self._logs:
            stream.close()
        self._logs = ()

    def _redraw(self) -> None:
        try:
            import bpy

            wm = getattr(bpy.context, "window_manager", None)
            if wm is None:
                return
            for win in wm.windows:
                for area in win.screen.areas:
                    if area.type in {"VIEW_3D", "SEQUENCE_EDITOR", "NODE_EDITOR", "IMAGE_EDITOR"}:
                        area.tag_redraw()
        except Exception:
            pass

    def _tick(self) -> Optional[float]:
        if not self.running or self._proc is None:
            return None

        status_data = self._read_json(self._status_file)
        if isinstance(status_data, dict):
            if "detail" in status_data:
                self.detail = str(status_data["detail"])
            if "progress" in status_data:
                try:
                    self.progress = max(0.0, min(0.99, float(status_data["progress"])))
                except (TypeError, ValueError):
                    pass
        else:
            elapsed = time.monotonic() - self._started_at
            self.progress = min(0.92, 0.12 + 0.78 * (1.0 - 1.0 / (1.0 + elapsed * 0.35)))
            self.detail = f"后台计算中… ({elapsed:.1f}s)"

        code = self._proc.poll()
        theme = get_theme(self.domain)
        if code is None:
            HUDManager.show(
                self.domain,
                f"{theme.tab_label} · {self.label}",
                self.detail,
                progress=self.progress,
                kind="INFO",
            )
            self._redraw()
            return self.poll_interval

        # Subprocess finished
        try:
            stdout, stderr = (self._tail(stream) for stream in self._logs)
        finally:
            self._close_logs()
        result_data = self._read_json(self._result_file) or {}
        if "returncode" not in result_data:
            result_data["returncode"] = code
        if stdout and "stdout_tail" not in result_data:
            result_data["stdout_tail"] = stdout
        if stderr and "stderr_tail" not in result_data:
            result_data["stderr_tail"] = stderr

        ok = code == 0 and result_data.get("ok", True) is not False
        self.running = False
        self.progress = 1.0 if ok else self.progress
        self._proc = None
        self._unwatch(timer_running=True)
        HUDManager.clear(self.domain)

        callback = self._on_complete
        self._on_complete = None
        if callback is not None:
            try:
                callback(ok, result_data)
            except Exception:
                pass
        self._redraw()
        return None

    def cancel(self) -> bool:
        if not self.running:
            return False
        proc = self._proc
        self.running = False
        self._proc = None
        self._on_complete = None
        self._unwatch()
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._close_logs()
        HUDManager.clear(self.domain)
        self.detail = "已取消后台任务"
        self.progress = 0.0
        self._redraw()
        return True

    def cleanup(self) -> None:
        if not self.cancel() and (self._timer_fn is not None or self._load_pre_fn is not None):
            self._unwatch()
