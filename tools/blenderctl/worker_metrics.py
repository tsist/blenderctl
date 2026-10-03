# SPDX-License-Identifier: GPL-3.0-or-later
"""Observed process peak RSS/working set, never a memory limit."""
import sys
from protocol import Failure


def peak_memory():
    if sys.platform == 'linux':
        import resource
        # Linux getrusage reports ru_maxrss in KiB, for this worker only.
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    if sys.platform != 'win32':
        raise Failure('UNSUPPORTED', 'Worker memory observation supports Windows and Linux')
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize',
                       'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                       'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    p = ctypes.WinDLL('psapi', use_last_error=True)
    p.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    p.GetProcessMemoryInfo.restype = wintypes.BOOL
    c = Counters(); c.cb = ctypes.sizeof(c)
    if not p.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise Failure('IO_ERROR', 'Cannot measure worker peak working set')
    return c.PeakWorkingSetSize
