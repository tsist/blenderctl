# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned AppContainer journal and explicit crash recovery. No PID-based killing.

Lifecycle lock precedes intent and profile creation, and remains held through
Job termination/profile deletion. Recovery never adopts old unproven journals.
Trusted host/current user and local same-Windows-session operation are assumed.
"""
import ctypes as c,os,re,time,uuid
from pathlib import Path
from protocol import Failure,atomic_json,read_json
from filesystem import kernel,native_path
from directory_handles import open_root
from resource_leases import process_identity,job_active,session_id
from win_appcontainer import api,sid_text,current_user_sid,Container

def derived_sid(name):
    _,a,u=api();p=c.c_void_p();hr=u.DeriveAppContainerSidFromAppContainerName(name,c.byref(p))
    if hr:raise OSError(f'Derive AppContainer SID failed: {hr:#x}')
    try:return sid_text(p)
    finally:a.FreeSid(p)

def profile_folder(sid):
    u=api()[2];u.GetAppContainerFolderPath.argtypes=[c.c_wchar_p,c.POINTER(c.c_void_p)];u.GetAppContainerFolderPath.restype=c.c_long
    p=c.c_void_p();hr=u.GetAppContainerFolderPath(sid,c.byref(p))&0xffffffff
    if hr in (0x80070002,0x80070003):return None
    if hr:raise Failure('CONFLICT',f'Cannot establish AppContainer profile presence: {hr:#x}')
    try:return c.wstring_at(p)
    finally:
        ole=c.WinDLL('ole32');ole.CoTaskMemFree.argtypes=[c.c_void_p];ole.CoTaskMemFree(p)

def profile_marker(sid):
    # Windows stores DisplayName and Moniker as part of CreateProfile itself.
    # Reading OS metadata closes success-before-journal and name-collision gaps.
    # Missing/changed metadata fails closed. This is not same-user authentication.
    import winreg
    key=r'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key+'\\'+sid) as h:
            return winreg.QueryValueEx(h,'Moniker')[0],winreg.QueryValueEx(h,'DisplayName')[0]
    except OSError as ex:raise Failure('CONFLICT','Profile ownership marker cannot be verified; retained') from ex

class Journal:
    def __init__(self,job):
        self.job=Path(job).absolute();self.file=self.job/'isolation-state.json';self.handle=None;self.state=None
        if not re.fullmatch('[0-9a-f]{32}',self.job.name):raise Failure('INVALID_REQUEST','Isolation recovery requires a job ID directory')
        h=open_root(self.job);kernel().CloseHandle(h)
        k=kernel();self.handle=k.CreateFileW(native_path(self.job/'isolation-lifecycle.lock'),0xc0000000,0,None,4,0x00200080,None)
        if self.handle==c.c_void_p(-1).value:
            self.handle=None;raise Failure('CONFLICT','Isolation lifecycle is active or its lock cannot be acquired')
        # Lock is outside the sandbox, never inherited; host metadata is trusted.
        try:
            request=read_json(self.job/'request.json')
            if request.get('command')!='extension.run' or request.get('params',{}).get('manifest',{}).get('isolation')!='WINDOWS_APPCONTAINER_V1':
                raise Failure('INVALID_REQUEST','Job does not describe the supported isolation profile')
        except BaseException:self.close();raise

    def save(self,**fields):
        self.state.update(fields);atomic_json(self.file,self.state)

    def create(self):
        if self.file.exists():raise Failure('CONFLICT','Isolation lifecycle record already exists; do not restart the same job')
        name='blenderctl.job.'+self.job.name;sid=derived_sid(name)
        if profile_folder(sid) is not None:raise Failure('CONFLICT','Profile predates this job; refusing to adopt or delete it')
        self.state={'version':'1.1','profile':'WINDOWS_APPCONTAINER_V1','job_directory':str(self.job),'appcontainer_name':name,'sid':sid,'owner':process_identity(os.getpid()),'user_sid':current_user_sid(),'session_id':session_id(),'job_name':'Local\\blenderctl-isolation-'+self.job.name,'cleanup':'pending','phase':'intent','profile_absent_before_create':True,'created_at':time.time()}
        self.state['ownership_marker']='blenderctl-owned-'+uuid.uuid4().hex
        self.save()  # Durable intent BEFORE the OS side effect.
        try:container=Container(name,lpac=False,display_name=self.state['ownership_marker'])
        except BaseException:
            self.save(phase='creation_failed');raise
        self.save(phase='created')
        if profile_marker(sid)!=(name,self.state['ownership_marker']):
            container.close();raise Failure('CONFLICT','OS profile ownership marker differs after creation')
        return container

    def set_job(self,name):
        self.save(job_name=name,phase='launch_intent')  # BEFORE atomic Job creation/spawn.

    def finished(self,ok,error=None):
        if self.state:
            atomic_json(self.job/'isolation-cleanup.json',{'ok':ok,'appcontainer_name':self.state['appcontainer_name'],'error':error,'recovered':False})
            self.save(cleanup='complete' if ok else 'pending',phase='complete' if ok else 'cleanup_failed')

    def close(self):
        if self.handle:kernel().CloseHandle(self.handle);self.handle=None

def recover(job):
    journal=Journal(job)
    try:
        if not journal.file.is_file():raise Failure('CONFLICT','No durable profile ownership journal; nothing automatically deleted')
        r=read_json(journal.file);journal.state=r
        name='blenderctl.job.'+journal.job.name
        required=('owner','user_sid','session_id','job_name','phase','cleanup','sid','ownership_marker')
        if r.get('version')!='1.1' or any(x not in r for x in required) or r.get('appcontainer_name')!=name or r.get('job_directory')!=str(journal.job) or r.get('profile_absent_before_create') is not True:
            raise Failure('CONFLICT','Legacy or inconsistent profile journal lacks ownership proof; retained')
        if r['user_sid']!=current_user_sid() or r['session_id']!=session_id() or r['sid']!=derived_sid(name):
            raise Failure('CONFLICT','Profile user/session/SID differs; retained')
        allowed_job=r['job_name']=='Local\\blenderctl-isolation-'+journal.job.name
        lease_file=journal.job/'resource-lease.json'
        if lease_file.is_file():
            lease=read_json(lease_file);allowed_job=allowed_job or (r['job_name']==lease.get('job_name')=='Local\\blenderctl-lease-'+str(lease.get('lease_id')))
        if not allowed_job:raise Failure('CONFLICT','Unbound Job name; profile retained')
        if r['cleanup']=='complete':
            if profile_folder(r['sid']) is not None:raise Failure('CONFLICT','Completed profile name is present again; not adopted or deleted')
            return {'ok':True,'already_complete':True,'appcontainer_name':name}
        owner=r['owner']
        if not isinstance(owner,dict) or set(owner)!={'pid','created'} or type(owner['pid']) is not int or type(owner['created']) is not int:
            raise Failure('CONFLICT','Invalid recorded process identity; retained')
        current=process_identity(owner['pid'])
        if current==owner:raise Failure('CONFLICT','Recorded supervisor is still alive; profile retained')
        if job_active(r['job_name'])!=0:raise Failure('CONFLICT','Owned Job still has active processes; profile retained')
        present=profile_folder(r['sid']) is not None
        if present:
            if not re.fullmatch('blenderctl-owned-[0-9a-f]{32}',str(r['ownership_marker'])) or profile_marker(r['sid'])!=(name,r['ownership_marker']):
                raise Failure('CONFLICT','OS profile marker differs from this job; retained')
            hr=api()[2].DeleteAppContainerProfile(name)&0xffffffff
            if hr not in (0,0x80070002,0x80070003):raise Failure('IO_ERROR',f'Owned profile deletion failed: {hr:#x}; retry journal retained')
            if profile_folder(r['sid']) is not None:raise Failure('IO_ERROR','Profile still present after deletion; retry journal retained')
        proof={'supervisor_identity_ended':True,'recorded_owner':owner,'current_pid_identity':current,'job_active_processes':0,'profile_was_present':present,'exclusive_lifecycle_lock':True,'os_ownership_marker_verified':present}
        result={'ok':True,'appcontainer_name':name,'recovered':True,'proof':proof}
        atomic_json(journal.job/'isolation-cleanup.json',result)
        journal.save(cleanup='complete',phase='complete',recovered_at=time.time());return result
    except Failure as ex:
        atomic_json(journal.job/'isolation-cleanup-attempt.json',{'ok':False,'at':time.time(),'error':{'code':ex.code,'message':str(ex)},'journal_preserved':True})
        raise
    finally:journal.close()
