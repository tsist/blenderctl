# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit-only Windows IO lease for a fixed v1 intersection-image result bundle.

The caller owns root authorization, pinned reader injection and final acceptance.
Directory membership is resampled, never claimed to be permanently frozen.
Existing FileGuard handles retain actual objects; no new native lock layer.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import hashlib
import os
import re
import threading
import uuid

from filesystem import FileGuard, kernel
import directory_handles as _directories
from protocol import Failure

_ROOT_FILES=frozenset(('receipt.json','eligible.jsonl','visibility.jsonl','PIPELINE.json',
                      'v.rgba.npy','h.rgba.npy','both.rgba.npy','off.rgba.npy'))
_PAGE=re.compile(r'[0-9]{6}\.(?:pairs\.u64|exact\.jsonl|kind\.npy|route\.npy|selectors\.npy|facts_valid\.npy|facts_value\.npy|fact_route\.npy|reason_mask\.npy)\Z',re.ASCII)
_HARD={'members':4096,'artifact_bytes':4*1024**3,'receipt_bytes':8*1024**2,'chunk_bytes':1024**2}

def _need(ok,code,message):
    if not ok:raise Failure(code,message)

def _identity(value):
    _need(type(value) is tuple and len(value)==2 and
          all(type(x) is int and 0<=x<2**64 for x in value),
          'INVALID_REQUEST','Identity must be an immutable native (volume,file_index) pair')
    return value

def _root_path(value):
    _need(type(value) is str,'INVALID_REQUEST','Native root path string required')
    try:value.encode('utf-8',errors='strict')
    except UnicodeEncodeError:raise Failure('INVALID_REQUEST','Root must be strict UTF-8')
    _need(value and not any(ord(c)<32 or ord(c)==127 for c in value),
          'INVALID_REQUEST','Root contains controls or is empty')
    text=value.replace('/','\\')
    _need(re.match(r'^[A-Za-z]:\\',text) is not None and ':' not in text[2:],
          'UNSUPPORTED','Only local absolute drive paths without ADS/device/UNC are supported')
    parts=text[3:].split('\\')
    if parts and parts[-1]=='':parts.pop()
    _need(all(part and part not in ('.','..') and not part.endswith(('.', ' ')) for part in parts),
          'UNSUPPORTED','Root components must be canonical without dot/parent/empty/trailing-dot-space')
    return Path(str(PureWindowsPath(text)))

@dataclass(frozen=True,slots=True)
class _Meta:
    size:int
    volume:int
    file_index:int
    link_count:int
    attributes:int
    @property
    def identity(self):return (self.volume,self.file_index)

class _CheckpointCall:
    """Retain callback identity across generator StopIteration/PEP479 edges."""
    def __init__(self,callback):self.callback=callback;self.error=None
    def __call__(self):
        self.error=None
        try:self.callback()
        except BaseException as error:
            self.error=error;raise

class WindowsBundleLease:
    """One-use context manager. Host ownership and parent acceptance are external.

    reader_api is a trusted already-selected module supplying MemberInfo and
    Inventory. The module/types are captured here, never read from a receipt.
    limits exposes members/artifact_bytes/receipt_bytes/chunk_bytes; only these
    IO limits are consumed. FileGuard's native prefetch stays <=1MiB even when
    the yielded chunk limit is smaller. This object is not an OS sandbox.
    """
    def __init__(self,root,*,expected_root_identity,forbidden_file_identities,
                 reader_api,limits,checkpoint):
        self._root=_root_path(root)
        self._expected_root_identity=_identity(expected_root_identity)
        _need(type(forbidden_file_identities) is frozenset,'INVALID_REQUEST','Frozen forbidden identity set required')
        self._forbidden=frozenset(_identity(x) for x in forbidden_file_identities)
        self._MemberInfo=getattr(reader_api,'MemberInfo',None)
        self._Inventory=getattr(reader_api,'Inventory',None)
        _need(isinstance(self._MemberInfo,type) and isinstance(self._Inventory,type),
              'INVALID_REQUEST','Trusted reader MemberInfo/Inventory classes required')
        self._limits={}
        for key,cap in _HARD.items():
            v=getattr(limits,key,None)
            _need(type(v) is int and 0<=v<=cap and (key!='chunk_bytes' or v>0),
                  'INVALID_REQUEST','IO limits may only tighten '+key)
            self._limits[key]=v
        _need(callable(checkpoint),'INVALID_REQUEST','Checkpoint callback required')
        self._checkpoint=checkpoint
        self._lock=threading.RLock()
        self._state='new';self._stack=None;self._generation=0
        self._ancestors=[];self._dir_guards={};self._files={};self._metadata={}
        self._captured_entries=None;self._hashes={};self._active={};self._exclusive=False
        self._id=None

    def _point(self):
        self._checkpoint()
        _need(self._state in ('opening','open'),'CONFLICT','Bundle lease is closed')

    def _open_required(self):
        _need(self._state=='open','CONFLICT','Bundle lease is not open')

    @property
    def lease_id(self):
        with self._lock:
            self._open_required();return self._id

    @staticmethod
    def _meta(handle):
        # Borrow the existing FileGuard information implementation for this
        # exact handle; no extra path open/stat or new native structure.
        borrowed=SimpleNamespace(k=kernel(),handle=handle)
        i=FileGuard._information(borrowed)
        return _Meta((i.size_high<<32)|i.size_low,i.volume,
                     (i.index_high<<32)|i.index_low,i.links,i.attributes)

    def _directory_meta(self,handle):
        m=self._meta(handle)
        _need(m.attributes&0x10 and not m.attributes&0x400,
              'CONFLICT','Expected a held ordinary non-reparse directory')
        return m

    def _same_directory(self,guard,enum_handle):
        held=self._directory_meta(guard.handle);opened=self._directory_meta(enum_handle)
        _need(held.identity==opened.identity,'CONFLICT','Enumeration handle directory identity differs')

    def __enter__(self):
        with self._lock:
            _need(self._state=='new','CONFLICT','Bundle lease cannot be reused')
            self._state='opening';self._stack=ExitStack()
            try:
                self._point()
                for path in (*reversed(self._root.parents),self._root):
                    self._point();g=self._stack.enter_context(FileGuard(path,directory=True))
                    m=self._directory_meta(g.handle);self._ancestors.append((path,g,m.identity))
                root_guard=self._ancestors[-1][1]
                _need(self._ancestors[-1][2]==self._expected_root_identity,'CONFLICT','Trusted root identity mismatch')
                self._dir_guards['']=root_guard
                initial=self._scan(acquire_pages=True)
                total=0;ids=set()
                for name,(attributes,size) in sorted(initial.items()):
                    self._point()
                    g=self._stack.enter_context(FileGuard(self._root/Path(name)))
                    m=self._meta(g.handle)
                    _need(not m.attributes&(0x10|0x400) and m.link_count==1,
                          'CONFLICT','Bundle file must be ordinary and single-link')
                    _need(m.identity not in ids and m.identity not in self._forbidden,
                          'CONFLICT','Bundle file identity aliases output or forbidden input')
                    _need((m.attributes,m.size)==(attributes,size),'CONFLICT','Member changed during acquisition')
                    ids.add(m.identity);self._files[name]=g;self._metadata[name]=m
                    if name=='receipt.json':
                        _need(m.size<=self._limits['receipt_bytes'],'RESOURCE_LIMIT','Receipt size exceeds lease limit')
                    else:
                        total+=m.size
                        _need(total<=self._limits['artifact_bytes'],'RESOURCE_LIMIT','Artifact size exceeds lease limit')
                self._captured_entries=initial
                _need(self._scan()==initial,'CONFLICT','Membership changed during acquisition')
                self._id='windows-bundle-lease.v1:'+uuid.uuid4().hex
                self._state='open'
                self.inventory()
                return self
            except BaseException:
                self.close();raise

    def _scan(self,acquire_pages=False):
        """Only root/pages enumeration; unknown entries are rejected, not read."""
        self._point();found={};entry_count=0;root_names=set();folded=set()
        enumeration_check=_CheckpointCall(self._point)
        h=_directories.open_root(self._root)
        try:
            self._same_directory(self._dir_guards[''],h)
            for name,attributes,size in _directories.entries(h,enumeration_check):
                self._point();entry_count+=1
                _need(entry_count<=self._limits['members']+1,'RESOURCE_LIMIT','Directory entry limit exceeded')
                _need(type(name) is str and name.isascii() and name.casefold() not in folded,
                      'CONFLICT','Noncanonical/case-aliased bundle member')
                folded.add(name.casefold());root_names.add(name)
                _need(not attributes&0x400,'CONFLICT','Bundle reparse entry forbidden')
                if attributes&0x10:
                    _need(name=='pages','CONFLICT','Unknown bundle directory')
                else:
                    _need(name in _ROOT_FILES,'CONFLICT','Unknown root file')
                    found[name]=(attributes,size)
            _need(root_names==set(_ROOT_FILES)|{'pages'},'CONFLICT','Required root files/pages missing')
            if acquire_pages:
                self._point();g=self._stack.enter_context(FileGuard(self._root/'pages',directory=True))
                self._directory_meta(g.handle);self._dir_guards['pages']=g
            _need('pages' in self._dir_guards,'CONFLICT','Pages guard missing')
            child=_directories.open_child(h,'pages')
            _need(child is not None,'CONFLICT','Pages directory disappeared')
            try:
                self._same_directory(self._dir_guards['pages'],child)
                folded=set()
                for name,attributes,size in _directories.entries(child,enumeration_check):
                    self._point();entry_count+=1
                    _need(entry_count<=self._limits['members']+1,'RESOURCE_LIMIT','Directory entry limit exceeded')
                    _need(type(name) is str and name.isascii() and name.casefold() not in folded,
                          'CONFLICT','Noncanonical/case-aliased page member')
                    folded.add(name.casefold())
                    _need(not attributes&(0x400|0x10),'CONFLICT','Nested directory/reparse member forbidden')
                    _need(name=='manifest.json' or _PAGE.fullmatch(name) is not None,'CONFLICT','Unknown page file')
                    found['pages/'+name]=(attributes,size)
                _need('pages/manifest.json' in found,'CONFLICT','Pages manifest missing')
            finally:kernel().CloseHandle(child)
        except BaseException:
            if enumeration_check.error is not None:raise enumeration_check.error
            raise
        finally:kernel().CloseHandle(h)
        _need(len(found)<=self._limits['members'],'RESOURCE_LIMIT','File member limit exceeded')
        return found

    def inventory(self):
        with self._lock:
            self._open_required();self._point()
            _need(self._scan()==self._captured_entries,'CONFLICT','Bundle membership/metadata changed')
            for path,g,identity in self._ancestors:
                self._point()
                _need(self._directory_meta(g.handle).identity==identity,'CONFLICT','Held ancestor changed')
                with FileGuard(path,directory=True) as fresh:
                    _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Ancestor path changed')
            # pages is not a root ancestor, so it has its own live/path check.
            g=self._dir_guards['pages'];identity=self._directory_meta(g.handle).identity
            with FileGuard(self._root/'pages',directory=True) as fresh:
                _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Pages path changed')
            values=[]
            for name,g in sorted(self._files.items()):
                self._point();current=self._meta(g.handle)
                _need(current==self._metadata[name],'CONFLICT','Held member metadata changed')
                with FileGuard(self._root/Path(name)) as fresh:
                    _need(self._meta(fresh.handle)==current,'CONFLICT','Member path identity/metadata changed')
                values.append(self._MemberInfo(relative_name=name,size=current.size,volume=current.volume,
                    file_index=current.file_index,link_count=current.link_count))
            self._point()
            return self._Inventory(files=tuple(values),directories=('pages',))

    def chunks(self,relative_name):
        with self._lock:
            self._open_required()
            _need(type(relative_name) is str and relative_name in self._files,'CONFLICT','Uncaptured bundle member')
            _need(not self._exclusive and relative_name not in self._active,
                  'CONFLICT','A member chunk iterator or hash recheck is already active')
            self._point();iterator=_Chunks(self,relative_name,self._generation)
            self._active[relative_name]=iterator
            return iterator

    def recheck(self):
        with self._lock:
            self._open_required()
            _need(not self._exclusive and not self._active,'CONFLICT','Recheck requires no active chunk iterators')
            self._exclusive=True
            try:
                self.inventory()
                for name,expected in self._hashes.items():
                    self._point();check=_CheckpointCall(self._point)
                    try:actual=self._files[name].sha256(check)
                    except BaseException:
                        if check.error is not None:raise check.error
                        raise
                    _need(actual==expected,'CONFLICT','Captured content hash changed')
                return self.inventory()
            finally:self._exclusive=False

    def close(self):
        with self._lock:
            if self._state=='closed':return
            self._state='closed';self._generation+=1
            for iterator in tuple(self._active.values()):iterator._release(invalidate=True)
            self._active.clear()
            if self._stack is not None:self._stack.close()

    def __exit__(self,*_):self.close()

class _Chunks:
    def __init__(self,lease,name,generation):
        self._lease=lease;self._name=name;self._generation=generation
        self._check=_CheckpointCall(lease._point)
        self._native=lease._files[name].chunks(self._check)
        self._buffer=b'';self._offset=0;self._count=0;self._digest=hashlib.sha256()
        self._done=False;self._invalid=False;self._executing=False

    def __iter__(self):return self

    def _release(self,invalidate=False):
        if invalidate:self._invalid=True
        if self._lease._active.get(self._name) is self:self._lease._active.pop(self._name)
        if not self._executing:self._native.close()

    def close(self):
        with self._lease._lock:
            self._done=True;self._release()

    def __next__(self):
        lease=self._lease
        with lease._lock:
            lease._open_required()
            _need(not self._invalid and self._generation==lease._generation,
                  'CONFLICT','Chunk iterator lease is closed')
            if self._done:raise StopIteration
            try:
                self._check()
                if self._offset>=len(self._buffer):
                    self._executing=True
                    try:self._buffer=next(self._native)
                    finally:self._executing=False
                    self._offset=0
                    _need(type(self._buffer) is bytes and 0<len(self._buffer)<=1024**2,
                          'CONFLICT','Unexpected FileGuard block')
                lease._open_required()
                end=min(len(self._buffer),self._offset+lease._limits['chunk_bytes'])
                result=self._buffer[self._offset:end];self._offset=end
                self._count+=len(result);self._digest.update(result)
                _need(self._count<=lease._metadata[self._name].size,'CONFLICT','Member grew during read')
                return result
            except StopIteration:
                if self._check.error is not None:
                    self._done=True;self._release();raise self._check.error
                try:
                    _need(self._count==lease._metadata[self._name].size,'CONFLICT','Member size differs at EOF')
                    lease._hashes[self._name]=self._digest.hexdigest();self._done=True
                finally:self._release()
                raise
            except BaseException:
                self._done=True;self._release()
                if self._check.error is not None:raise self._check.error
                raise
