# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned process-tree lifetime. Never kill a process looked up by name or stale PID."""
import ctypes
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, command, memory_bytes=None, job_name=None, sandbox=None, **kwargs):
        self.handle = None
        if os.name == "nt":
            from ctypes import wintypes as w
            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
            self.extended_type = Extended
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            k = self.kernel
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            k.CreateJobObjectW.restype = w.HANDLE
            k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            k.SetInformationJobObject.restype = w.BOOL
            k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
            k.QueryInformationJobObject.restype = w.BOOL
            k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            k.AssignProcessToJobObject.restype = w.BOOL
            k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
            k.TerminateJobObject.restype = w.BOOL
            k.CloseHandle.argtypes = [w.HANDLE]
            k.CloseHandle.restype = w.BOOL
            self.handle = k.CreateJobObjectW(None, job_name)
            if self.handle and job_name and ctypes.get_last_error()==183:
                self.close()
                raise RuntimeError('Owned Job name already exists; refusing to attach')
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if sandbox:
                limits.BasicLimitInformation.LimitFlags |= 0x8
                limits.BasicLimitInformation.ActiveProcessLimit = 1
            if memory_bytes is not None:
                limits.BasicLimitInformation.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY: committed bytes, entire tree
                limits.JobMemoryLimit = memory_bytes
            if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())
            # Worker waits for the supervisor's gate before running any operation.
            try:
                self.process = sandbox.spawn(command, job_handle=self.handle, **kwargs) if sandbox else subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
                if not sandbox and not k.AssignProcessToJobObject(self.handle, w.HANDLE(int(self.process._handle))):
                    error = ctypes.WinError(ctypes.get_last_error())
                    self.process.kill()
                    self.process.wait()
                    raise error
                if sandbox:self.process.resume()
            except BaseException:
                self.close()
                raise
        else:
            if sandbox:raise RuntimeError('AppContainer requires Windows; no unsandboxed fallback')
            if memory_bytes is not None:raise RuntimeError('Hard job memory limits require Windows')
            self.process = subprocess.Popen(command, start_new_session=True, **kwargs)

    def usage(self):
        if not self.handle:return {}
        info=self.extended_type()
        if not self.kernel.QueryInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'peak_job_committed_bytes':info.PeakJobMemoryUsed,'job_memory_limit_bytes':info.JobMemoryLimit,'limit_flags':info.BasicLimitInformation.LimitFlags}

    def terminate(self):
        if os.name == "nt":
            if self.handle and not self.kernel.TerminateJobObject(self.handle, 7):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait(timeout=10)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        process=getattr(self,'process',None)
        if process and hasattr(process,'close'):process.close()
