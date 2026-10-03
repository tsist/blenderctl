# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-controlled staging and output reception for explicit Windows isolation.

The script controls all bytes in scratch, including its return value. Only the
host creates accepted metadata, after the process has exited. No auto-opening
of produced blend files; content safety and artistic quality are not asserted.
"""
import hashlib,json,os,stat
from pathlib import Path
from protocol import Failure,atomic_json,digest
from filesystem import FileGuard,native_path
from review_contract import canonical

PROFILE='WINDOWS_APPCONTAINER_V1'
MAX_OUTPUT=256*1024*1024
MAX_TOTAL_OUTPUT=512*1024*1024

def plain(path,root=None):
    path=Path(path).absolute()
    if root is not None and not path.is_relative_to(Path(root).absolute()):
        raise Failure('VALIDATION_FAILED','Sandbox path escapes owned area')
    for p in (path,*path.parents):
        info=Path(native_path(p)).lstat()
        if getattr(info,'st_file_attributes',0)&0x400:
            raise Failure('VALIDATION_FAILED','Reparse point rejected: '+str(p))
        if p==root:break
    return Path(native_path(path)).lstat()

class IsolatedJob:
    def __init__(self,job,request,launch,stack,checkpoint):
        self.container=None;self.lifecycle=None;self.job=job;self.request=request;self.launch=launch
        self.checkpoint=checkpoint;self.box=job/'isolation-box';self.scratch=self.box/'scratch'
        self.spec=request['params']['manifest'];self.prepared=[];self.process_evidence=None
        if os.name!='nt':raise Failure('UNSUPPORTED','Windows AppContainer requested on another OS; no fallback')
        from win_appcontainer import Container,secure_path
        plain(job);self.box.mkdir();self.scratch.mkdir()
        try:
            from isolation_lifecycle import Journal
            self.lifecycle=Journal(job);self.container=self.lifecycle.create()
            secure_path(self.box,self.box,self.container.sid_string)
            secure_path(self.scratch,self.box,self.container.sid_string,True)
            self.runtime=self.box/'runtime';self.runtime.mkdir()
            runtime_root=Path(launch['blender']).parent
            plain(runtime_root)
            # No user portable settings/add-ons. Every included byte is copied
            # through a denied-write handle into a new object, never hardlinked.
            pending=[runtime_root];total=0
            while pending:
                parent=pending.pop();checkpoint()
                for source in sorted(parent.iterdir()):
                    if source.name in ('portable','__pycache__') or source.suffix in ('.pyc','.blend','.blend1'):continue
                    info=plain(source,runtime_root);relative=source.relative_to(runtime_root);target=self.runtime/relative
                    if stat.S_ISDIR(info.st_mode):Path(native_path(target)).mkdir();pending.append(source)
                    elif stat.S_ISREG(info.st_mode):
                        total+=info.st_size
                        if total>2*1024**3 or len(self.prepared)>20000:raise Failure('UNSUPPORTED','Runtime exceeds isolated staging budget')
                        with FileGuard(source) as guard:self.copy(guard,target,'runtime/'+relative.as_posix())
                    else:raise Failure('UNSUPPORTED','Runtime contains non-regular objects')
            staged_exe=self.runtime/Path(launch['blender']).name
            if not staged_exe.is_file():raise Failure('UNSUPPORTED','Blender entry excluded from isolated runtime')
            # Review binds the actual installed executable; input/source guards
            # remain held by the supervisor throughout execution and reception.
            from reviews import verify_run
            self.authorization=verify_run(request['params'],launch['blender'],stack,checkpoint)
            source=self.spec['script'];script=self.box/'script.py'
            with FileGuard(source['file']) as guard:
                actual=self.copy(guard,script,'script.py')
            if actual!=source['expected_sha256']:raise Failure('CONFLICT','Staged script differs from requested hash')
            inputs=self.box/'inputs';inputs.mkdir();mapping={}
            for i,document in enumerate(request['params'].get('resources',[])):
                target=inputs/(str(i)+Path(document['file']).suffix)
                with FileGuard(document['file']) as guard:actual=self.copy(guard,target,'inputs/'+target.name)
                if actual!=document['expected_sha256']:raise Failure('CONFLICT','Staged input differs from requested hash')
                mapping[document['file']]=str(target)
            entry=self.box/'entry.py'
            with FileGuard(Path(__file__).with_name('isolated_script_worker.py')) as guard:self.copy(guard,entry,'entry.py')
            internal={'script':str(script),'script_sha256':source['expected_sha256'],'scratch':str(self.scratch),'input_files':mapping,'params':self.spec['params']}
            canonical(internal);atomic_json(self.box/'input.json',internal)
            self.prepared.append({'relative':'input.json','sha256':digest(self.box/'input.json')})
            # The environment is deliberately built afresh: no tokens, proxy
            # variables, Python hooks, user add-on paths or caller PATH.
            system=os.environ.get('SystemRoot',r'C:\Windows')
            from isolation_lifecycle import profile_folder
            local_base=profile_folder(self.lifecycle.state['sid'])
            if local_base is None:raise Failure('IO_ERROR','Owned AppContainer storage is unavailable')
            redirected_temp=local_base+'\\Packages\\'+self.container.name+'\\AC\\Temp\\'
            if len(redirected_temp.encode('utf-16-le'))//2>=260:
                raise Failure('UNSUPPORTED','Owned AppContainer temp redirection exceeds verified Windows path bound')
            self.env={'SystemRoot':system,'WINDIR':system,'SystemDrive':Path(system).drive,
                      'PATH':str(self.runtime)+';'+system+r'\System32',
                      'TEMP':str(self.scratch),'TMP':str(self.scratch),'USERPROFILE':str(self.scratch),
                      'LOCALAPPDATA':local_base,'APPDATA':str(self.scratch),
                      'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
            for name in ('CONFIG','SCRIPTS','EXTENSIONS','DATAFILES','RESOURCES'):
                path=self.scratch/name.lower();path.mkdir();self.env['BLENDER_USER_'+name]=str(path)
            threads=launch.get('limits',{}).get('cpu_threads',2)
            self.command=[str(staged_exe),'--background','--factory-startup','--disable-autoexec','--offline-mode',
                          '--threads',str(threads),'--python-exit-code','23','--python',str(entry),'--',str(self.box/'input.json')]
            self.report={'profile':PROFILE,'os_enforced':True,'lpac':False,'network_capabilities':[],
                         'child_processes':'denied','staging_manifest':self.prepared,'runtime_bytes':total,
                         'input_files':mapping,'write_scope':'owned scratch plus OS-created per-job AppContainer profile storage',
                         'read_scope':'staged runtime/input/code plus Windows resources already granted to AppContainers',
                         'environment':'fresh allowlist; no caller secrets','temp_redirection_base':local_base,'output_trust':'all script bytes and return values are untrusted',
                         'authorization_limitations_scope':'review records only; these do not assert the runtime sandbox',
                         'limits':'No hard VRAM/disk quota; no VM/kernel-exploit boundary; trusted host/runtime and OS configuration assumed'}
            checkpoint()
        except BaseException:
            try:self.close()
            except Exception:pass  # cleanup failure is preserved separately
            raise

    def copy(self,guard,target,relative):
        self.checkpoint();h=hashlib.sha256()
        with Path(native_path(target)).open('xb') as out:
            for block in guard.chunks(self.checkpoint):out.write(block);h.update(block)
        result=h.hexdigest()
        if digest(native_path(target),self.checkpoint)!=result:raise Failure('CONFLICT','Isolated copy verification failed')
        self.prepared.append({'relative':relative,'sha256':result});return result

    def launched(self,process):
        self.process_evidence=process.isolation_evidence
        self.lifecycle.save(phase='running',worker_pid=process.pid)
        atomic_json(self.job/'isolation-report.json',{**self.report,'process':self.process_evidence,'phase':'running'})

    def accept(self):
        plain(self.scratch,self.box)
        # At this point the only process allowed in the Job has exited. Never
        # trust a sandbox-provided file list, SHA or worker-result status.
        outputs=[];total=0
        for name in self.spec['outputs']:
            source=self.scratch/name
            try:info=plain(source,self.box)
            except FileNotFoundError as ex:raise Failure('VALIDATION_FAILED','Declared isolated output is missing: '+name) from ex
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise Failure('VALIDATION_FAILED','Output must be a regular single-link file')
            total+=info.st_size
            if info.st_size>MAX_OUTPUT or total>MAX_TOTAL_OUTPUT:raise Failure('RESOURCE_LIMIT','Isolated output byte budget exceeded')
            if source.suffix=='.json':
                if info.st_size>4*1024**2:raise Failure('RESOURCE_LIMIT','Isolated JSON exceeds 4 MiB')
                try:canonical(json.loads(source.read_text(encoding='utf-8')))
                except (ValueError,UnicodeError,RecursionError) as ex:raise Failure('VALIDATION_FAILED','Invalid isolated JSON') from ex
            target=self.job/name
            with FileGuard(source) as guard:
                h=hashlib.sha256()
                with target.open('xb') as out:
                    for block in guard.chunks(self.checkpoint):out.write(block);h.update(block)
            actual=digest(target,self.checkpoint)
            if actual!=h.hexdigest():raise Failure('CONFLICT','Accepted output copy differs')
            outputs.append({'file':str(target),'sha256':actual,'bytes':info.st_size})
        returned=self.scratch/'script-return.json'
        try:info=plain(returned,self.box)
        except FileNotFoundError as ex:raise Failure('VALIDATION_FAILED','Isolated script return is missing') from ex
        if info.st_nlink!=1 or not stat.S_ISREG(info.st_mode) or info.st_size>4*1024**2:raise Failure('VALIDATION_FAILED','Invalid script return object')
        try:claimed=json.loads(returned.read_text(encoding='utf-8'));canonical(claimed)
        except (ValueError,UnicodeError,RecursionError) as ex:raise Failure('VALIDATION_FAILED','Invalid script return JSON') from ex
        if not isinstance(claimed,dict) or set(claimed)!={'result'}:raise Failure('VALIDATION_FAILED','Invalid script return shape')
        isolation={**self.report,'process':self.process_evidence,'phase':'accepted'}
        atomic_json(self.job/'isolation-report.json',isolation)
        result={'extension_report_version':'1.1','script':self.spec['script'],'script_sha256_observed':self.spec['script']['expected_sha256'],
                'outputs':outputs,'result':claimed['result'],'result_trust':'untrusted_script_claim',
                'authorization':self.authorization,'isolation':isolation,
                'execution':'Windows AppContainer execution; host-generated metadata and copied output SHA; content/semantic safety not certified'}
        atomic_json(self.job/'extension-report.json',result);return result

    def close(self):
        try:
            if self.container:
                try:
                    from isolation_lifecycle import profile_marker
                    if profile_marker(self.lifecycle.state['sid'])!=(self.lifecycle.state['appcontainer_name'],self.lifecycle.state['ownership_marker']):
                        raise Failure('CONFLICT','Profile ownership changed before normal cleanup; retained')
                    self.container.close()
                    from isolation_lifecycle import profile_folder
                    if profile_folder(self.lifecycle.state['sid']) is not None:raise OSError('Profile remains after deletion')
                    self.lifecycle.finished(True)
                except Exception as ex:
                    self.lifecycle.finished(False,str(ex));raise
                finally:self.container=None
        finally:
            if self.lifecycle:self.lifecycle.close();self.lifecycle=None
