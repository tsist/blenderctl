# SPDX-License-Identifier: GPL-3.0-or-later
"""Win32 isolated-desktop process launcher with Job Object cleanup guard.

Safe to import on non-Windows hosts (e.g. Linux CI); Win32 APIs are only bound on Windows.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from typing import Sequence


_DESKTOP_ALL_ACCESS = 0x000F01FF
_CREATE_NO_WINDOW = 0x08000000
_CREATE_SUSPENDED = 0x00000004
_WAIT_OBJECT_0 = 0x00000000
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9

if os.name == "nt":
    from ctypes import wintypes

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]


def _win_apis():
    if os.name != "nt":
        raise RuntimeError("Isolated desktop execution requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return user32, kernel32


def run_on_isolated_desktop(
    argv: Sequence[str],
    *,
    desktop_prefix: str = "BlenderPreview",
    timeout_seconds: float = 90.0,
    log_path: str | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    """Run one Blender GUI process on a private hidden Windows Desktop and wait until exit.

    Never calls SwitchDesktop; the user's active desktop and focus are never touched.
    """
    if os.name != "nt":
        raise RuntimeError("Isolated desktop execution requires Windows")

    user32, kernel32 = _win_apis()
    desk_name = f"{desktop_prefix}_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    hdesk = user32.CreateDesktopW(desk_name, None, None, 0, _DESKTOP_ALL_ACCESS, None)
    if not hdesk:
        raise OSError(f"CreateDesktopW({desk_name!r}) failed with Win32 error {ctypes.get_last_error()}")

    hjob = kernel32.CreateJobObjectW(None, None)
    if hjob:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            hjob,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

    h_std = None
    inherit = False
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.lpDesktop = desk_name

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ_WRITE = 0x00000003
        CREATE_ALWAYS = 2
        FILE_ATTRIBUTE_NORMAL = 0x80
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        h_std = kernel32.CreateFileW(
            str(log_path),
            GENERIC_WRITE,
            FILE_SHARE_READ_WRITE,
            ctypes.byref(sa),
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if h_std and h_std != wintypes.HANDLE(-1).value:
            STARTF_USESTDHANDLES = 0x00000100
            si.dwFlags |= STARTF_USESTDHANDLES
            si.hStdOutput = h_std
            si.hStdError = h_std
            inherit = True
        else:
            h_std = None

    pi = _PROCESS_INFORMATION()
    cmd_str = subprocess.list2cmdline([str(x) for x in argv])
    cmd_buf = ctypes.create_unicode_buffer(cmd_str)
    flags = _CREATE_NO_WINDOW | _CREATE_SUSPENDED

    t0 = time.monotonic()
    ok = kernel32.CreateProcessW(
        None,
        cmd_buf,
        None,
        None,
        inherit,
        flags,
        None,
        cwd,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        err = ctypes.get_last_error()
        if h_std:
            kernel32.CloseHandle(h_std)
        if hjob:
            kernel32.CloseHandle(hjob)
        user32.CloseDesktop(hdesk)
        raise OSError(f"CreateProcessW on isolated desktop failed with Win32 error {err}")

    timed_out = False
    exit_code = -1
    pid = int(pi.dwProcessId)
    try:
        if hjob:
            kernel32.AssignProcessToJobObject(hjob, pi.hProcess)
        kernel32.ResumeThread(pi.hThread)
        wait_ms = max(1000, int(timeout_seconds * 1000))
        res = kernel32.WaitForSingleObject(pi.hProcess, wait_ms)
        if res != _WAIT_OBJECT_0:
            timed_out = True
            kernel32.TerminateProcess(pi.hProcess, 124)
            kernel32.WaitForSingleObject(pi.hProcess, 5000)
        code_val = wintypes.DWORD(0)
        if kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code_val)):
            exit_code = int(code_val.value)
    finally:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        if h_std:
            kernel32.CloseHandle(h_std)
        if hjob:
            kernel32.CloseHandle(hjob)
        user32.CloseDesktop(hdesk)

    return {
        "pid": pid,
        "desktop": desk_name,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - t0, 3),
    }


def spawn_detached_desktop_host(
    argv: Sequence[str],
    *,
    desktop_name: str,
    log_path: str | None = None,
) -> tuple[int, int, int]:
    """Start a persistent hidden-desktop Blender process (caller manages lifetime).

    Returns (pid, hProcess, hDesktop). Caller must close handles after process exits.
    """
    if os.name != "nt":
        raise RuntimeError("Isolated desktop execution requires Windows")

    user32, kernel32 = _win_apis()
    hdesk = user32.CreateDesktopW(desktop_name, None, None, 0, _DESKTOP_ALL_ACCESS, None)
    if not hdesk:
        raise OSError(f"CreateDesktopW({desktop_name!r}) failed: err={ctypes.get_last_error()}")

    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.lpDesktop = desktop_name
    inherit = False
    h_std = None

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        h_std = kernel32.CreateFileW(
            str(log_path), 0x40000000, 0x00000003, ctypes.byref(sa), 2, 0x80, None
        )
        if h_std and h_std != wintypes.HANDLE(-1).value:
            si.dwFlags |= 0x00000100
            si.hStdOutput = h_std
            si.hStdError = h_std
            inherit = True
        else:
            h_std = None

    pi = _PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(x) for x in argv]))
    ok = kernel32.CreateProcessW(
        None,
        cmd_buf,
        None,
        None,
        inherit,
        _CREATE_NO_WINDOW,
        None,
        None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if h_std:
        kernel32.CloseHandle(h_std)
    if not ok:
        err = ctypes.get_last_error()
        user32.CloseDesktop(hdesk)
        raise OSError(f"CreateProcessW failed: err={err}")

    kernel32.CloseHandle(pi.hThread)
    return int(pi.dwProcessId), int(pi.hProcess), int(hdesk)
