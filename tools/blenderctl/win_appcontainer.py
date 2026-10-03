# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows AppContainer primitives. No global ACL changes.

Only newly owned staging paths may be granted access. The caller must attach a
suspended process to its Job, inspect the token, then resume it. No fallback.
"""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import time

P = c.c_void_p
SZ = c.c_size_t

class STARTUPINFO(c.Structure):
    _fields_ = [('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
                ('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),
                ('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),('dwFillAttribute',w.DWORD),
                ('dwFlags',w.DWORD),('wShowWindow',w.WORD),('cbReserved2',w.WORD),('lpReserved2',P),
                ('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]

class STARTUPINFOEX(c.Structure):
    _fields_ = [('StartupInfo',STARTUPINFO),('lpAttributeList',P)]

class PROCESSINFO(c.Structure):
    _fields_ = [('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]

class CAPS(c.Structure):
    _fields_ = [('AppContainerSid',P),('Capabilities',P),('CapabilityCount',w.DWORD),('Reserved',w.DWORD)]

def api():
    if os.name != 'nt':
        raise OSError('AppContainer requires Windows; no unsandboxed fallback')
    k=c.WinDLL('kernel32',use_last_error=True)
    a=c.WinDLL('advapi32',use_last_error=True)
    u=c.WinDLL('userenv',use_last_error=True)
    signatures=[
        (k,'GetCurrentProcess',[],w.HANDLE),
        (k,'CloseHandle',[w.HANDLE],w.BOOL),
        (k,'LocalFree',[P],P),
        (k,'DuplicateHandle',[w.HANDLE,w.HANDLE,w.HANDLE,c.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD],w.BOOL),
        (k,'InitializeProcThreadAttributeList',[P,w.DWORD,w.DWORD,c.POINTER(SZ)],w.BOOL),
        (k,'UpdateProcThreadAttribute',[P,w.DWORD,SZ,P,SZ,P,P],w.BOOL),
        (k,'DeleteProcThreadAttributeList',[P],None),
        (k,'CreateProcessW',[w.LPCWSTR,w.LPWSTR,P,P,w.BOOL,w.DWORD,P,w.LPCWSTR,P,c.POINTER(PROCESSINFO)],w.BOOL),
        (k,'ResumeThread',[w.HANDLE],w.DWORD),
        (k,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD),
        (k,'GetExitCodeProcess',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
        (k,'TerminateProcess',[w.HANDLE,w.UINT],w.BOOL),
        (a,'OpenProcessToken',[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)],w.BOOL),
        (a,'GetTokenInformation',[w.HANDLE,c.c_int,P,w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
        (a,'ConvertSidToStringSidW',[P,c.POINTER(w.LPWSTR)],w.BOOL),
        (a,'ConvertStringSecurityDescriptorToSecurityDescriptorW',[w.LPCWSTR,w.DWORD,c.POINTER(P),P],w.BOOL),
        (a,'GetSecurityDescriptorDacl',[P,c.POINTER(w.BOOL),c.POINTER(P),c.POINTER(w.BOOL)],w.BOOL),
        (a,'GetSecurityDescriptorSacl',[P,c.POINTER(w.BOOL),c.POINTER(P),c.POINTER(w.BOOL)],w.BOOL),
        (a,'SetNamedSecurityInfoW',[w.LPWSTR,c.c_int,w.DWORD,P,P,P,P],w.DWORD),
        (a,'FreeSid',[P],P),
        (u,'DeriveAppContainerSidFromAppContainerName',[w.LPCWSTR,c.POINTER(P)],c.c_long),
        (u,'CreateAppContainerProfile',[w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,P,w.DWORD,c.POINTER(P)],c.c_long),
        (u,'DeleteAppContainerProfile',[w.LPCWSTR],c.c_long),
    ]
    for dll,name,args,result in signatures:
        f=getattr(dll,name);f.argtypes=args;f.restype=result
    return k,a,u

def check(ok):
    if not ok:raise c.WinError(c.get_last_error())

def sid_text(sid):
    k,a,_=api();p=w.LPWSTR()
    check(a.ConvertSidToStringSidW(sid,c.byref(p)))
    try:return p.value
    finally:k.LocalFree(p)

def token_info(handle,kind):
    _,a,_=api();token=w.HANDLE();check(a.OpenProcessToken(handle,8,c.byref(token)))
    try:
        needed=w.DWORD();a.GetTokenInformation(token,kind,None,0,c.byref(needed))
        if not needed.value:raise c.WinError(c.get_last_error())
        buf=c.create_string_buffer(needed.value)
        check(a.GetTokenInformation(token,kind,buf,needed.value,c.byref(needed)))
        return buf
    finally:api()[0].CloseHandle(token)

def current_user_sid():
    k,_,_=api();buf=token_info(k.GetCurrentProcess(),1)
    return sid_text(c.cast(buf,c.POINTER(P))[0])

def token_evidence(handle):
    app=token_info(handle,29)
    result={'is_appcontainer':bool(c.cast(app,c.POINTER(w.DWORD))[0])}
    if result['is_appcontainer']:
        buf=token_info(handle,31)
        result['appcontainer_sid']=sid_text(c.cast(buf,c.POINTER(P))[0])
        caps=token_info(handle,30)
        result['capability_count']=c.cast(caps,c.POINTER(w.DWORD))[0]
        integrity=token_info(handle,25)
        result['integrity_sid']=sid_text(c.cast(integrity,c.POINTER(P))[0])
    return result

def secure_path(path,owned_root,sid,write=False):
    """Replace ACL on an existing owned non-reparse object, inheritable for dirs."""
    path=Path(path).absolute();root=Path(owned_root).resolve(strict=True)
    if not path.is_relative_to(root):raise ValueError('ACL target outside owned staging root')
    for p in (path,*path.parents):
        if getattr(p.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('Reparse ACL target/ancestor rejected: '+str(p))
        if p==root:break
    k,a,_=api();descriptor=P();dacl=P();sacl=P();present=w.BOOL();default=w.BOOL()
    rights='0x1301bf' if write else '0x1200a9'
    flags='OICI' if path.is_dir() else ''
    sddl=f'D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;{current_user_sid()})(A;{flags};{rights};;;{sid})S:(ML;{flags};NW;;;LW)'
    check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(descriptor),None))
    try:
        check(a.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(dacl),c.byref(default)))
        check(a.GetSecurityDescriptorSacl(descriptor,c.byref(present),c.byref(sacl),c.byref(default)))
        # Ownership implies WRITE_DAC, not WRITE_OWNER (required to lower MIC).
        # Grant the supervisor full access to its NEW object before setting MIC.
        for flags,da,sa in ((0x80000004,dacl,None),(0x10,None,sacl)):
            error=a.SetNamedSecurityInfoW(str(path),1,flags,None,None,da,sa)
            if error:raise c.WinError(error)
    finally:k.LocalFree(descriptor)

class NativeProcess:
    def __init__(self,pi):
        self._handle=pi.hProcess;self._thread=pi.hThread;self.pid=pi.dwProcessId;self.returncode=None

    def resume(self):
        k,_,_=api()
        if k.ResumeThread(self._thread)==0xffffffff:raise c.WinError(c.get_last_error())
        k.CloseHandle(self._thread);self._thread=None

    def poll(self):
        if self.returncode is not None:return self.returncode
        k,_,_=api();status=k.WaitForSingleObject(self._handle,0)
        if status==0x102:return None
        if status!=0:raise c.WinError(c.get_last_error())
        code=w.DWORD();check(k.GetExitCodeProcess(self._handle,c.byref(code)));self.returncode=code.value
        return self.returncode

    def wait(self,timeout=None):
        deadline=time.monotonic()+timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic()>=deadline:raise subprocess.TimeoutExpired('AppContainer',timeout)
            time.sleep(.025)
        return self.returncode

    def kill(self):
        check(api()[0].TerminateProcess(self._handle,7))

    def close(self):
        k,_,_=api()
        if self._thread:k.CloseHandle(self._thread);self._thread=None
        if self._handle:k.CloseHandle(self._handle);self._handle=None

class Container:
    def __init__(self,name,lpac=True,detached=True,display_name=None):
        self.k,self.a,self.u=api();self.sid=P();self.lpac=lpac;self.name=name;self.detached=detached
        hr=self.u.CreateAppContainerProfile(name,display_name or name,'Transient Blender CLI isolated worker',None,0,c.byref(self.sid))
        if hr:raise OSError(f'CreateAppContainerProfile failed: {hr:#x}; no existing profile reused')
        self.sid_string=sid_text(self.sid)

    def close(self):
        if self.sid:self.a.FreeSid(self.sid);self.sid=P()
        if self.name:
            hr=self.u.DeleteAppContainerProfile(self.name)
            if hr:raise OSError(f'Delete owned AppContainer profile failed: {hr:#x}')
            self.name=None

    def spawn(self,command,*,cwd,env,stdin,stdout,stderr,job_handle=None):
        import msvcrt
        k=self.k;size=SZ();attribute_count=(4 if self.lpac else 3)+(1 if job_handle else 0)
        k.InitializeProcThreadAttributeList(None,attribute_count,0,c.byref(size))
        storage=c.create_string_buffer(size.value)
        check(k.InitializeProcThreadAttributeList(storage,attribute_count,0,c.byref(size)))
        inherited=[];null=None;pi=PROCESSINFO()
        try:
            def attribute(key,value):
                check(k.UpdateProcThreadAttribute(storage,0,key,c.byref(value),c.sizeof(value),None,None))
            caps=CAPS(self.sid,None,0,0);attribute(0x20009,caps)
            if job_handle:
                jobs=(w.HANDLE*1)(job_handle);attribute(0x2000d,jobs)
            policy=w.DWORD(1);attribute(0x2000e,policy)
            if self.lpac:attribute(0x2000f,policy)
            if stdin==subprocess.DEVNULL:null=open(os.devnull,'rb');stdin=null
            for stream in (stdin,stdout,stderr):
                dup=w.HANDLE()
                check(k.DuplicateHandle(k.GetCurrentProcess(),w.HANDLE(msvcrt.get_osfhandle(stream.fileno())),k.GetCurrentProcess(),c.byref(dup),0,True,2))
                inherited.append(dup.value)
            handles=(w.HANDLE*3)(*inherited);attribute(0x20002,handles)
            si=STARTUPINFOEX();si.StartupInfo.cb=c.sizeof(si);si.lpAttributeList=c.cast(storage,P)
            si.StartupInfo.dwFlags=0x100;si.StartupInfo.hStdInput,si.StartupInfo.hStdOutput,si.StartupInfo.hStdError=inherited
            environment=c.create_unicode_buffer('\0'.join(f'{key}={value}' for key,value in sorted(env.items(),key=lambda kv:kv[0].upper()))+'\0\0')
            line=c.create_unicode_buffer(subprocess.list2cmdline([str(x) for x in command]))
            window_flag=8 if self.detached else 0x8000000
            check(k.CreateProcessW(str(command[0]),line,None,None,True,window_flag|0x80000|0x400|4,environment,str(cwd),c.byref(si),c.byref(pi)))
            process=NativeProcess(pi)
            try:
                evidence=token_evidence(process._handle)
                if evidence!={'is_appcontainer':True,'appcontainer_sid':self.sid_string,'capability_count':0,'integrity_sid':'S-1-16-4096'}:
                    raise RuntimeError('Unexpected AppContainer token: '+repr(evidence))
                k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)];k.IsProcessInJob.restype=w.BOOL
                member=w.BOOL()
                if not job_handle:raise RuntimeError('Isolated worker requires an atomic Job assignment')
                check(k.IsProcessInJob(process._handle,job_handle,c.byref(member)))
                if not member.value:raise RuntimeError('Isolated process is not in the required Job')
                process.isolation_evidence={**evidence,'lpac_requested':self.lpac,'child_process_policy':'RESTRICTED','inherited_handles':'stdin/stdout/stderr_only','created_suspended':True,'job_assigned_at_creation':bool(job_handle),'console':'DETACHED_PROCESS' if self.detached else 'CREATE_NO_WINDOW'}
                process.isolation_evidence['job_membership_verified']=True
            except BaseException:
                process.kill();process.wait(10);process.close();raise
            return process
        finally:
            for handle in inherited:k.CloseHandle(handle)
            if null:null.close()
            k.DeleteProcThreadAttributeList(storage)
