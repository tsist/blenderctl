# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit candidate: one native child stage inside an existing parent supervisor.

No job system, daemon, sandbox or acceptance authority is created here. Caller
owns command authorization, broader implementation closure and stage namespace.
"""
from contextlib import ExitStack
from pathlib import Path
import ctypes
from ctypes import wintypes as w
import json
import os
import re
import subprocess
import time

from filesystem import FileGuard
from processes import ProcessTree
from protocol import Failure

def _need(ok,message):
    if not ok:raise Failure('INVALID_REQUEST',message)

def _text(value,label):
    _need(type(value) is str and '\0' not in value,label+' must be a native NUL-free string')
    try:value.encode('utf-8',errors='strict')
    except UnicodeEncodeError:raise Failure('INVALID_REQUEST',label+' must be strict UTF-8')
    return value

def _absolute(value,label,*,native_path=False):
    if native_path:
        _need(isinstance(value,Path),label+' must be a Path')
        value=str(value)
    _text(value,label);path=Path(value)
    _need(path.is_absolute(),label+' must be absolute')
    return path

def _key(path):return os.path.normcase(os.path.normpath(str(path)))

def _validate(stage_dir,command,bootstrap_path,python_executable,env,documents,memory_bytes,checkpoint):
    _need(os.name=='nt','Owned native stage currently requires Windows')
    _need(isinstance(stage_dir,Path),'stage_dir must be a Path')
    stage=_absolute(stage_dir,'stage_dir',native_path=True)
    _need(type(command) is tuple and len(command)>0,'command must be a nonempty tuple')
    for arg in command:_text(arg,'command argument')
    executable=_absolute(command[0],'command[0]')
    bootstrap=_absolute(bootstrap_path,'bootstrap_path',native_path=True)
    python=_absolute(python_executable,'python_executable',native_path=True)
    _need(type(env) is dict,'env must be a native dict')
    copied_env={}
    for name,value in env.items():
        _text(name,'environment name');_text(value,'environment value')
        _need(name and '=' not in name[1:],'Environment names must be nonempty and contain no internal equals sign')
        copied_env[name]=value
    _need(callable(checkpoint),'checkpoint callback required')
    _need(memory_bytes is None or type(memory_bytes) is int and 0<memory_bytes<2**63,
          'memory_bytes must be None or a positive native signed-64-bit byte count')
    _need(type(documents) is tuple and documents,'expected_documents must be a nonempty tuple')
    descriptors={}
    for row in documents:
        _need(type(row) is dict and set(row)=={'file','expected_sha256'},'Exact document descriptor fields required')
        path=_absolute(row['file'],'document file')
        digest=row['expected_sha256']
        _need(type(digest) is str and re.fullmatch('[0-9a-f]{64}',digest) is not None,'Lower-case document SHA256 required')
        key=_key(path);_need(key not in descriptors,'Duplicate lexical document descriptor')
        descriptors[key]=(path,digest)
    required={_key(bootstrap),_key(python),_key(executable)}
    i=1
    while i<len(command):
        arg=command[i]
        if arg=='--python':
            _need(i+1<len(command),'--python requires an absolute script path')
            required.add(_key(_absolute(command[i+1],'--python script')));i+=2;continue
        if arg.startswith('--python='):
            required.add(_key(_absolute(arg.partition('=')[2],'--python script')))
        i+=1
    _need(required<=set(descriptors),'Required bootstrap/python/executable/--python code anchor is missing')
    # Read-only preflight; every parameter has been validated before any write.
    _need(stage.is_dir() and not any(stage.iterdir()),'stage_dir must already exist and be empty')
    return stage,bootstrap,python,copied_env,tuple(descriptors.values())

def _error(error):
    try:message=str(error)
    except BaseException:message='<exception message unavailable>'
    code=getattr(error,'code',None)
    return dict(type=type(error).__name__,code=code if type(code) is str else None,message=message[:4096])

def _hash(guard,checkpoint):
    # Existing FileGuard.chunks is a generator. Preserve callback identity even
    # when it raises StopIteration and Python's PEP479 wraps the generator.
    caught=[]
    def check():
        try:checkpoint()
        except BaseException as error:caught.append(error);raise
    try:return guard.sha256(check)
    except BaseException:
        if caught:raise caught[-1]
        raise

def _accounting(tree):
    class Accounting(ctypes.Structure):
        _fields_=[('TotalUserTime',ctypes.c_int64),('TotalKernelTime',ctypes.c_int64),
            ('ThisPeriodTotalUserTime',ctypes.c_int64),('ThisPeriodTotalKernelTime',ctypes.c_int64),
            ('TotalPageFaultCount',w.DWORD),('TotalProcesses',w.DWORD),('ActiveProcesses',w.DWORD),('TotalTerminatedProcesses',w.DWORD)]
    info=Accounting()
    if not tree.kernel.QueryInformationJobObject(tree.handle,1,ctypes.byref(info),ctypes.sizeof(info),None):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(total_processes=info.TotalProcesses,active_processes=info.ActiveProcesses,
                total_terminated_processes=info.TotalTerminatedProcesses)

def _cleanup(tree,lifecycle):
    """Only this returned ProcessTree. A ten-second bound is for reap, not stage."""
    failures=[]
    def attempt(label,fn):
        try:return fn()
        except BaseException as error:
            failures.append((error,error.__traceback__));lifecycle['cleanup_errors'].append(dict(step=label,**_error(error)))
            return None
    lifecycle['cleanup_attempted']=True
    usage=attempt('usage_before_terminate',tree.usage)
    if usage is not None:lifecycle['usage']=usage
    counts=attempt('accounting_before_terminate',lambda:_accounting(tree))
    if counts is not None:lifecycle['job_accounting_before_cleanup']=counts
    try:
        attempt('terminate_owned_tree',tree.terminate)
        attempt('reap_owned_root',lambda:tree.process.wait(timeout=10))
        def zero_active():
            deadline=time.monotonic()+10
            while True:
                result=_accounting(tree)
                if result['active_processes']==0:return result
                if time.monotonic()>=deadline:raise Failure('WORKER_FAILED','Owned Job did not reach zero active processes during cleanup')
                time.sleep(.02)
        counts=attempt('wait_owned_job_zero',zero_active)
        if counts is not None:
            lifecycle['job_accounting_after_terminate']=counts
            lifecycle['owned_job_zero_active_observed']=True
        usage=attempt('usage_after_terminate',tree.usage)
        if usage is not None:lifecycle['usage']=usage
    finally:
        attempt('close_owned_tree_handle',tree.close)
        lifecycle['tree_handle_closed']=tree.handle is None
        observed=attempt('poll_root_after_close',tree.process.poll)
        if observed is None:attempt('reap_root_after_close',lambda:tree.process.wait(timeout=10))
        lifecycle['root_returncode']=attempt('poll_root_final',tree.process.poll)
    return failures

def _record(stage,lifecycle):
    data=(json.dumps(lifecycle,ensure_ascii=True,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    with (stage/'lifecycle.json').open('xb') as stream:
        stream.write(data);stream.flush()

def run_stage(stage_dir,*,command:tuple[str,...],checkpoint,bootstrap_path:Path,
              python_executable:Path,env:dict[str,str],expected_documents:tuple,
              memory_bytes=None):
    """Run one owned stage; never publish job/GUI acceptance.

    Required descriptors cover bootstrap/python/command[0] and --python scripts.
    Caller must supply all other applicable code/dependency anchors, including
    a Python positional script, and owns ancestor/path authorization. FileGuard
    protects the provided file objects; this helper is not an OS sandbox.
    """
    stage,bootstrap,python,child_env,documents=_validate(stage_dir,command,bootstrap_path,
        python_executable,env,expected_documents,memory_bytes,checkpoint)
    checkpoint() # pre-start cancellation: no new files and no child process.
    lifecycle=dict(schema='toon-image-owned-stage-lifecycle.v1',scope='owned_native_stage_only',
        stage_dir=str(stage),command=list(command),bootstrap_path=str(bootstrap),python_executable=str(python),
        environment_values_recorded=False,environment_entry_count=len(child_env),shell=False,
        tree_create_attempted=False,tree_created=False,gate_released=False,cleanup_attempted=False,
        root_pid=None,root_returncode_before_cleanup=None,root_returncode=None,
        tree_handle_closed=None,tree_handle_closed_meaning='ProcessTree.handle is None after close',
        owned_job_zero_active_observed=False,independent_descendant_PID_verification=False,
        usage={},cleanup_errors=[],source_recheck_passed=False,source_documents=[],
        parent_job_finalized=False,os_sandbox=False,status='running')
    with ExitStack() as guards:
        held=[];tree=None;stdout=None;stderr=None;wrote=False;primary=None;primary_tb=None
        try:
            for path,expected in documents:
                checkpoint();guard=guards.enter_context(FileGuard(path));actual=_hash(guard,checkpoint)
                if actual!=expected:raise Failure('CONFLICT','Initial stage document SHA mismatch: '+str(path))
                identity=guard.identity();held.append((path,expected,guard,identity))
                lifecycle['source_documents'].append(dict(file=str(path),expected_sha256=expected,initial_sha256=actual,identity=identity))
            directory=guards.enter_context(FileGuard(stage,directory=True))
            info=directory._information()
            if not info.attributes&0x10:raise Failure('CONFLICT','Stage is not a directory')
            _need(not any(stage.iterdir()),'stage_dir changed or is not empty')
            checkpoint()
            gate=stage/'gate';gate.mkdir();wrote=True
            stdout=(stage/'stdout.log').open('xb');stderr=(stage/'stderr.log').open('xb')
            launch=(str(python),'-I','-B',str(bootstrap),str(gate),*command)
            lifecycle['launch_command']=list(launch);lifecycle['tree_create_attempted']=True
            tree=ProcessTree(list(launch),memory_bytes=memory_bytes,shell=False,stdin=subprocess.DEVNULL,
                stdout=stdout,stderr=stderr,cwd=str(stage),env=child_env)
            lifecycle['tree_created']=True;lifecycle['root_pid']=tree.process.pid
            checkpoint()
            with (gate/'worker.gate').open('xb'):pass
            lifecycle['gate_released']=True
            while tree.process.poll() is None:
                checkpoint();time.sleep(.1)
            lifecycle['root_returncode_before_cleanup']=tree.process.returncode
            checkpoint() # natural root exit observation, before interpreting its code.
            if tree.process.returncode!=0:raise Failure('WORKER_FAILED','Native stage exited with code '+str(tree.process.returncode))
        except BaseException as error:
            primary=error;primary_tb=error.__traceback__
        finally:
            if tree is not None:
                cleanup=_cleanup(tree,lifecycle)
                if primary is None and cleanup:primary,primary_tb=cleanup[0]
            for label,stream in [('stdout',stdout),('stderr',stderr)]:
                if stream is not None:
                    try:stream.close()
                    except BaseException as error:
                        lifecycle['cleanup_errors'].append(dict(step='close_'+label,**_error(error)))
                        if primary is None:primary=error;primary_tb=error.__traceback__
        # Guards still held here, after the owned tree has been terminated,
        # reaped and closed. Do not repeatedly call a cancelling checkpoint
        # while preserving the first failure during diagnostic source recheck.
        try:
            for index,(path,expected,guard,identity) in enumerate(held):
                actual=_hash(guard,checkpoint if primary is None else lambda:None)
                if guard.identity()!=identity or actual!=expected:raise Failure('CONFLICT','Stage document changed: '+str(path))
                with FileGuard(path) as fresh:
                    if fresh.identity()!=identity:raise Failure('CONFLICT','Stage document path identity changed: '+str(path))
                lifecycle['source_documents'][index]['final_sha256']=actual
            lifecycle['source_recheck_passed']=len(held)==len(documents)
            if primary is None:checkpoint() # final caller cancellation/timeout gate.
        except BaseException as error:
            if primary is None:primary=error;primary_tb=error.__traceback__
            else:lifecycle['cleanup_errors'].append(dict(step='source_recheck',**_error(error)))
        if primary is None:
            if not (tree is not None and lifecycle['root_returncode']==0 and lifecycle['tree_handle_closed'] and lifecycle['owned_job_zero_active_observed']):
                primary=Failure('WORKER_FAILED','Owned stage lifecycle was not completely observed')
        lifecycle['status']='completed' if primary is None else 'failed'
        if primary is not None:lifecycle['primary_error']=_error(primary)
        if wrote:
            try:_record(stage,lifecycle)
            except BaseException as error:
                lifecycle['record_error']=_error(error)
                if primary is None:primary=error;primary_tb=error.__traceback__;lifecycle['status']='failed'
        if primary is not None:
            try:primary.stage_lifecycle=lifecycle
            except BaseException:pass
            raise primary.with_traceback(primary_tb)
        return lifecycle
