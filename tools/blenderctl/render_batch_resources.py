"""Best-effort Windows GPU admission and local storage estimates."""

import errno
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


MIB = 1024 * 1024
GPU_LIMIT_PERCENT = 75
VRAM_LIMIT_PERCENT = 90
ENGINE = re.compile(r"^pid_\d+_(luid_.*?_phys_\d+_eng_\d+)_engtype_(3D|Compute.*)$", re.I)


def command_output(command):
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
        timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def windows_engine_load():
    root = os.environ.get("SystemRoot")
    if not root:
        raise ValueError("Windows system path is unavailable")
    powershell = Path(root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "$ErrorActionPreference='Stop'; "
        "ConvertTo-Json -Compress -InputObject @(Get-CimInstance "
        "Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine "
        "| Select-Object Name,UtilizationPercentage)"
    )
    rows = json.loads(command_output([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command]))
    engines = {}
    for row in rows:
        match = ENGINE.fullmatch(row["Name"])
        if match:
            key = match.group(1).casefold()
            engines[key] = engines.get(key, 0) + int(row["UtilizationPercentage"])
    if not engines:
        raise ValueError("No 3D or compute counters returned by WDDM")
    return min(100, max(engines.values())), None, "Windows WDDM 3D/compute engine utilization"


def nvidia_load():
    nvidia = shutil.which("nvidia-smi")
    if not nvidia:
        raise ValueError("nvidia-smi is unavailable")
    output = command_output([
        nvidia, "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits",
    ])
    utilization = []
    capacity = []
    for line in output.splitlines():
        use, used, total = [value.strip() for value in line.split(",")]
        if use.isdigit():
            utilization.append(int(use))
        if used.isdigit() and total.isdigit() and int(total):
            capacity.append(round(int(used) * 100 / int(total)))
    if not utilization:
        raise ValueError("NVIDIA returned no usable utilization measurements")
    return max(utilization), max(capacity, default=None), "NVIDIA whole-device utilization and memory"


def gpu_sample(backend):
    # Prefer the device family actually used by the request. Graphics uses an
    # implicit host adapter and Windows counters cannot map all Blender IDs.
    observers = (nvidia_load, windows_engine_load) if backend in {"CUDA", "OPTIX"} else (windows_engine_load, nvidia_load)
    for observe in observers:
        try:
            use, memory, source = observe()
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            continue
        busy = use >= GPU_LIMIT_PERCENT or (memory is not None and memory >= VRAM_LIMIT_PERCENT)
        return {"state": "busy" if busy else "idle", "source": source, "utilization_percent": use,
                "memory_used_percent": memory, "scope": "highest observed adapter or engine"}
    return {"state": "unavailable", "reason": "Windows GPU engine and NVIDIA utilization counters unavailable"}


def disk_full(error):
    if isinstance(error, OSError) and (error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
                                       or getattr(error, "winerror", None) in {39, 112, 1295}):
        return True
    message = str(error).casefold()
    return any(phrase in message for phrase in (
        "no space left on device", "not enough space on the disk", "disk full", "disk is full",
        "free disk space below", "空间不足", "磁盘已满",
    ))


def space_shortfall(directory, reserve_mib, additional_bytes):
    required = reserve_mib * MIB + additional_bytes
    available = shutil.disk_usage(directory).free
    return max(0, (required - available + MIB - 1) // MIB)


def frame_space(dimensions):
    # RGBA scanlines plus PNG compression/container overhead. Stereo, EXR,
    # and higher bit depth are outside this batch delivery profile.
    width, height = dimensions
    return (width * 4 + 1) * height + 65536


def memory_admission(size, pass_count, source_bytes, backend, *, composite):
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length',ctypes.c_uint32),('load',ctypes.c_uint32),
                    *((name,ctypes.c_uint64) for name in ('physical','available','page','page_available',
                                                        'virtual','virtual_available','extended'))]
    status=MemoryStatus()
    status.length=ctypes.sizeof(status)
    host_available=None
    if os.name=='nt' and ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        host_available=status.available//MIB
    nvidia=shutil.which('nvidia-smi')
    gpu_available=None
    if nvidia and backend in ('CUDA','OPTIX'):
        try:
            values=command_output([nvidia,'--query-gpu=memory.free','--format=csv,noheader,nounits'])
            rows=[int(value) for value in values.splitlines() if value.strip().isdigit()]
            gpu_available=min(rows) if rows else None
        except (OSError,subprocess.SubprocessError):
            pass
    pixels=size[0]*size[1]
    pass_buffers=max(1,pass_count)*pixels*16
    beauty_buffers=(6 if composite else 3)*pixels*16
    source_budget=source_bytes*4
    host_estimate=1024+(2*pass_buffers+beauty_buffers+source_budget)//MIB
    gpu_estimate=1024+(pass_buffers+beauty_buffers+source_budget)//MIB
    return {'method':'conservative pass/beauty buffers plus four times saved project bytes and a 1 GiB base reserve',
            'host_available_mb':host_available,'estimated_working_set_mb':host_estimate,
            'gpu_available_mb':gpu_available,'estimated_gpu_mb':gpu_estimate if backend != 'CPU' else 0,
            'admitted':(host_available is None or host_available>=host_estimate)
                        and (gpu_available is None or gpu_available>=gpu_estimate)}
