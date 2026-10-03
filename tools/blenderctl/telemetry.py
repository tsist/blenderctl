# SPDX-License-Identifier: GPL-3.0-or-later
"""Observations are not limits. No GPU or filesystem configuration is modified."""
import ctypes,os,shutil,subprocess
from ctypes import wintypes as w

def memory_sample(tree):
    if not tree.handle:return {'available':False,'reason':'Windows Job required'}
    class Ids(ctypes.Structure):
        _fields_=[('assigned',w.DWORD),('count',w.DWORD),('ids',ctypes.c_size_t*1024)]
    ids=Ids();k=tree.kernel
    if not k.QueryInformationJobObject(tree.handle,3,ctypes.byref(ids),ctypes.sizeof(ids),None):return {'available':False,'reason':'process list unavailable'}
    from resource_leases import native
    k=native();ps=ctypes.WinDLL('psapi',use_last_error=True)
    class Memory(ctypes.Structure):
        _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(n,ctypes.c_size_t) for n in ('peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    ps.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.c_void_p,w.DWORD];ps.GetProcessMemoryInfo.restype=w.BOOL
    rss=private=sampled=0
    for pid in list(ids.ids)[:ids.count]:
        h=k.OpenProcess(0x1000|0x10,False,pid)
        if not h:continue
        try:
            m=Memory();m.cb=ctypes.sizeof(m)
            if ps.GetProcessMemoryInfo(h,ctypes.byref(m),m.cb):rss+=m.rss;private+=m.private;sampled+=1
        finally:k.CloseHandle(h)
    return {'available':True,'processes_assigned':ids.count,'processes_sampled':sampled,'rss_sum_bytes':rss,'private_committed_sum_bytes':private,'complete_snapshot':sampled==ids.count,'scope':'non-atomic process sample; shared resident pages may count more than once'}

def gpu_sample():
    exe=shutil.which('nvidia-smi')
    if not exe:return {'available':False,'reason':'nvidia-smi not found','hard_limit':False}
    try:
        p=subprocess.run([exe,'--query-gpu=uuid,memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if p.returncode:return {'available':False,'reason':'device query failed','hard_limit':False}
        rows=[]
        for line in p.stdout.splitlines():
            identity,used,total=[v.strip() for v in line.split(',')];rows.append({'uuid':identity,'used_mib':int(used),'total_mib':int(total)})
        return {'available':True,'devices':rows,'scope':'whole-device observation, includes other applications; not per-job allocation','hard_limit':False}
    except (OSError,ValueError,subprocess.TimeoutExpired):return {'available':False,'reason':'device sample unavailable','hard_limit':False}
