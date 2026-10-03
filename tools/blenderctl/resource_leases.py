# SPDX-License-Identifier: GPL-3.0-or-later
"""Cooperative same-Windows-session reservations, with owned Job crash recovery."""
import ctypes,os,time,uuid
from ctypes import wintypes as w
from pathlib import Path
from contextlib import contextmanager
from protocol import Failure,atomic_json,read_json
from filesystem import FileGuard,kernel,native_path
KEYS=('cpu_threads','memory_mb','gpu_mb')

def native():
    if os.name!='nt':raise Failure('UNSUPPORTED','Shared leases currently require Windows')
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    for name,args,ret in [('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),('GetProcessTimes',[w.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p],w.BOOL),('WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD),('CloseHandle',[w.HANDLE],w.BOOL),('OpenJobObjectW',[w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),('QueryInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p],w.BOOL)]:
        f=getattr(k,name);f.argtypes=args;f.restype=ret
    return k

def process_identity(pid):
    k=native();h=k.OpenProcess(0x1000|0x100000,False,pid)
    if not h:
        if ctypes.get_last_error()==87:return None
        raise Failure('CONFLICT','Cannot establish process identity; reservation retained')
    try:
        if k.WaitForSingleObject(h,0)==0:return None
        values=[ctypes.c_uint64() for _ in range(4)]
        if not k.GetProcessTimes(h,*[ctypes.byref(x) for x in values]):raise ctypes.WinError(ctypes.get_last_error())
        return {'pid':pid,'created':values[0].value}
    finally:k.CloseHandle(h)

def job_active(name):
    k=native();h=k.OpenJobObjectW(4,False,name)
    if not h:
        if ctypes.get_last_error()==2:return 0
        raise Failure('CONFLICT','Cannot query owned Job; reservation retained')
    class Accounting(ctypes.Structure):
        _fields_=[(x,ctypes.c_int64) for x in ('user','kernel','period_user','period_kernel')]+[(x,w.DWORD) for x in ('page_faults','total','active','terminated')]
    try:
        a=Accounting()
        if not k.QueryInformationJobObject(h,1,ctypes.byref(a),ctypes.sizeof(a),None):raise ctypes.WinError(ctypes.get_last_error())
        return a.active
    finally:k.CloseHandle(h)

@contextmanager
def locked(domain,checkpoint):
    k=kernel()
    while True:
        checkpoint()
        h=k.CreateFileW(native_path(Path(domain)/'admission.lock'),0xc0000000,0,None,4,0x80,None)
        if h!=ctypes.c_void_p(-1).value:break
        number=ctypes.get_last_error()
        if number not in (32,33):raise Failure('CONFLICT','Domain lock unavailable; admission denied: '+str(number))
        time.sleep(.05)
    try:yield
    finally:k.CloseHandle(h)

def session_id():
    k=native();k.ProcessIdToSessionId.argtypes=[w.DWORD,ctypes.POINTER(w.DWORD)];k.ProcessIdToSessionId.restype=w.BOOL
    s=w.DWORD()
    if not k.ProcessIdToSessionId(os.getpid(),ctypes.byref(s)):raise ctypes.WinError(ctypes.get_last_error())
    return s.value

def config(p):
    r=read_json(p/'domain.json')
    if r.get('session_id')!=session_id():raise Failure('UNSUPPORTED','Resource domain belongs to another Windows session; local Job names cannot be probed')
    return r

def domain_path(path):
    p=Path(path)
    if not p.is_absolute() or str(p).startswith('\\\\'):raise Failure('UNSUPPORTED','Domain requires a local absolute Windows path')
    if any(x.is_symlink() or x.is_junction() for x in [p,*p.parents]):raise Failure('UNSUPPORTED','Domain path cannot contain links')
    return p.resolve()

def init(path,budget,checkpoint):
    p=domain_path(path)
    if p.exists():raise Failure('CONFLICT','Resource domain must be a new directory')
    p.mkdir(parents=True,exist_ok=False)
    with locked(p,checkpoint):
        atomic_json(p/'domain.json',{'version':'1.0','id':uuid.uuid4().hex,'session_id':session_id(),'budget':budget,'scope':'cooperative pipelines in one Windows session; declared reservations, not CPU/VRAM isolation'})
        (p/'leases').mkdir()
    return read_json(p/'domain.json')

def sweep(p):
    active=[];cfg=config(p)
    for f in sorted((p/'leases').glob('*.json')):
        r=read_json(f)
        if r['state']=='released':continue
        if r['domain_id']!=cfg['id']:raise Failure('CONFLICT','Lease domain identity differs; capacity retained')
        owner=process_identity(r['owner']['pid']);live_job=job_active(r['job_name'])
        grace=r['state']!='reserved' or time.time()-r['created_at']>=15
        if not live_job and (r['state']=='release_requested' or owner!=r['owner'] and grace):
            r.update(state='released',released_at=time.time(),reason='no live owned Job and owner ended or released');atomic_json(f,r)
        else:active.append(r)
    return active

def status(path,checkpoint):
    p=domain_path(path)
    with locked(p,checkpoint):
        cfg=config(p);rows=sweep(p)
        return {'domain':str(p),'config':cfg,'active':rows,'reserved':{k:sum(r['limits'][k] for r in rows) for k in KEYS}}

class Lease:
    def __init__(self,path,limits,job,checkpoint):
        self.domain=domain_path(path);self.id=uuid.uuid4().hex;self.file=self.domain/'leases'/(self.id+'.json');self.job_name='Local\\blenderctl-lease-'+self.id
        self.config_guard=FileGuard(self.domain/'domain.json')
        try:self.acquire(limits,job,checkpoint)
        except BaseException:self.config_guard.close();raise

    def acquire(self,limits,job,checkpoint):
        owner=process_identity(os.getpid())
        while True:
            with locked(self.domain,checkpoint):
                cfg=config(self.domain);budget=cfg['budget']
                if any(limits[k]>budget[k] for k in KEYS):raise Failure('RESOURCE_LIMIT','Step exceeds shared resource domain capacity')
                rows=sweep(self.domain)
                if all(sum(r['limits'][k] for r in rows)+limits[k]<=budget[k] for k in KEYS):
                    self.record={'id':self.id,'domain_id':cfg['id'],'owner':owner,'child':None,'job_name':self.job_name,'job_directory':str(job),'limits':{k:limits[k] for k in KEYS},'state':'reserved','created_at':time.time()}
                    atomic_json(self.file,self.record);break
            checkpoint();time.sleep(.05)

    def started(self,pid,checkpoint):
        with locked(self.domain,checkpoint):
            self.record.update(state='running',child=process_identity(pid));atomic_json(self.file,self.record)

    def release(self):
        deadline=time.monotonic()+5
        def check():
            if time.monotonic()>deadline:raise Failure('CONFLICT','Lease release lock timeout; reservation retained')
        try:
            with locked(self.domain,check):
                self.record.update(state='release_requested');atomic_json(self.file,self.record);sweep(self.domain)
        finally:self.config_guard.close()
