"""Direct, bounded Windows Blender workers for native fluid transactions."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from filesystem import PathLocks
from processes import ProcessTree
from protocol import DEFAULT_JOBS, Failure


MIB = 1024 ** 2
GIB = 1024 ** 3
PROFILES = {"light": 768, "standard": 1536, "production": 6144}
PROFILE_THREADS = {"light": 2, "standard": 2, "production": 4}
DESKTOP_RESERVE = 6 * GIB
MIN_FREE_DISK = 30 * GIB
PROFILE_DISK = {"light": 512 * MIB, "standard": 512 * MIB, "production": 8 * GIB}
JOB_MEMORY = 0x200
LOW_PRIORITY = 0x4000


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("memory_load", wintypes.DWORD)] + [
        (name, ctypes.c_uint64) for name in ("physical_total", "physical_available", "commit_total",
                                            "commit_available", "virtual_total", "virtual_available", "extended")]


class BasicLimit(ctypes.Structure):
    _fields_ = [("per_process_time", ctypes.c_int64), ("per_job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t), ("maximum_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling_class", wintypes.DWORD)]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", BasicLimit), ("io_counters", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


def workstation_slot():
    # The canonical mutex path is the same even when work uses different jobs directories.
    return PathLocks([DEFAULT_JOBS.parent / "physics-native-workstation.lock"])


def kernel():
    if os.name != "nt":
        raise Failure("UNSUPPORTED", "Native fluid workers require Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def headroom(roots):
    windows = kernel()
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    windows.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    windows.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not windows.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"available_ram_bytes": memory.physical_available, "available_commit_bytes": memory.commit_available,
            "free_disk_bytes": min(shutil.disk_usage(root).free for root in roots)}


def check_worker_gate(gate, expected_limit, threads):
    deadline = time.monotonic() + 15
    while not Path(gate).is_file():
        if time.monotonic() >= deadline:
            raise Failure("RESOURCE_LIMIT", "The supervisor did not authorize the Blender process")
        time.sleep(0.05)
    windows = kernel()
    windows.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD, ctypes.c_void_p]
    windows.QueryInformationJobObject.restype = wintypes.BOOL
    limits = ExtendedLimit()
    if (not windows.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None)
            or not limits.basic.flags & JOB_MEMORY or limits.job_memory != expected_limit):
        raise Failure("RESOURCE_LIMIT", "Blender did not observe the exact hard memory limit on its own Windows Job")
    windows.GetCurrentProcess.restype = wintypes.HANDLE
    windows.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    windows.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                               ctypes.POINTER(ctypes.c_size_t)]
    windows.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    process = windows.GetCurrentProcess()
    allowed, system = ctypes.c_size_t(), ctypes.c_size_t()
    if (not windows.SetPriorityClass(process, LOW_PRIORITY)
            or not windows.GetProcessAffinityMask(process, ctypes.byref(allowed), ctypes.byref(system))):
        raise ctypes.WinError(ctypes.get_last_error())
    cpus = [i for i in range(allowed.value.bit_length()) if (allowed.value >> i) & 1]
    if len(cpus) < threads:
        raise Failure("RESOURCE_LIMIT", f"Native physics needs {threads} available logical processors")
    # Prefer alternate logical CPUs, as in the light profile, before filling available siblings.
    cpus = (cpus[::2] + cpus[1::2])[:threads]
    if not windows.SetProcessAffinityMask(process, sum(1 << index for index in cpus)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"job_memory_limit_bytes": limits.job_memory, "limit_flags": limits.basic.flags,
            "affinity": cpus, "thread_limit": threads, "priority": "below_normal", "before_scene_open": True}


def watch_bytes(paths):
    total = 0
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            total += path.stat().st_size
            continue
        for root, directories, names in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not
                              (getattr((Path(root) / name).lstat(), "st_file_attributes", 0) & 0x400)]
            for name in names:
                try:
                    data = (Path(root) / name).lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(data.st_mode):
                    total += data.st_size
    return total


def run_worker(blender, worker, job, phase, timeout, profile, watch_paths=(), cancel_check=None):
    limit = PROFILES[profile] * MIB
    threads = PROFILE_THREADS[profile]
    disk_limit = PROFILE_DISK[profile]
    start_ram = DESKTOP_RESERVE + limit + 512 * MIB
    paths = (job, *watch_paths)
    anchors = {path.anchor for path in paths}
    start = headroom(anchors)
    report = {"phase": phase, "launched": False, "resource_profile": profile, "thread_limit": threads,
              "job_disk_limit_bytes": disk_limit, "desktop_reserve_bytes": DESKTOP_RESERVE,
              "start_ram_bytes": start_ram, "initial_headroom": start, "job_memory_limit_bytes": limit,
              "peak_job_committed_bytes": 0, "returncode": 1, "timeout_seconds": timeout}
    peak_files = watch_bytes(paths)
    if (start["available_ram_bytes"] < start_ram or start["available_commit_bytes"] < 12 * GIB
            or start["free_disk_bytes"] < MIN_FREE_DISK or peak_files > disk_limit):
        return {**report, "stop_reason": "desktop_admission", "job_bytes": peak_files}

    gate = job / f".{phase}-approved"
    command = [str(blender), "--background", "--factory-startup", "--disable-autoexec", "--threads", str(threads),
               "--python-exit-code", "1", "--python", str(worker), "--", str(job / "request.json"),
               phase, str(gate), str(limit), str(threads)]
    env = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[key] = str(threads)
    stop = None
    low = dict(start)
    started = time.monotonic()
    with (job / f"{phase}.stdout.log").open("xb") as out, (job / f"{phase}.stderr.log").open("xb") as err:
        child = ProcessTree(command, memory_bytes=limit, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env)
        report["launched"] = True
        try:
            evidence = child.usage()
            if evidence["job_memory_limit_bytes"] != limit or not evidence["limit_flags"] & JOB_MEMORY:
                raise Failure("RESOURCE_LIMIT", "The supervisor could not verify the Blender process-tree hard cap")
            gate.touch(exist_ok=False)
            next_sample = started
            while child.process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout:
                    stop = "timeout"
                elif cancel_check and cancel_check():
                    stop = "cancelled"
                elif now >= next_sample:
                    next_sample = now + 2
                    sample = headroom(anchors)
                    for key, value in sample.items():
                        low[key] = min(low[key], value)
                    peak_files = max(peak_files, watch_bytes(paths))
                    if sample["available_ram_bytes"] < DESKTOP_RESERVE:
                        stop = "desktop_ram_reserve"
                    elif sample["available_commit_bytes"] < 8 * GIB:
                        stop = "desktop_commit_reserve"
                    elif sample["free_disk_bytes"] < MIN_FREE_DISK or peak_files > disk_limit:
                        stop = "disk_reserve_or_job_size"
                if stop:
                    child.terminate()
                    break
                time.sleep(0.20)
            report.update(child.usage())
            peak_files = max(peak_files, watch_bytes(paths))
            if not stop and peak_files > disk_limit:
                stop = "job_size"
            report.update(returncode=1 if stop else child.process.returncode,
                          blender_exit_code=child.process.returncode, minimum_headroom=low,
                          stop_reason=stop, wall_seconds=time.monotonic() - started, job_bytes=peak_files)
        finally:
            child.close()
            gate.unlink(missing_ok=True)
    return report
