# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit-only selected layout over the existing held-handle Windows lease.

No replacement file-reading or cleanup implementation; native identity and
recheck semantics are inherited. This is not public host/parent acceptance.
"""
from dataclasses import dataclass
from pathlib import Path
import re
from toon_image_bundle_lease import WindowsBundleLease, _need, _CheckpointCall
from filesystem import FileGuard, kernel
import directory_handles as directories

ROOT_FILES=frozenset(('receipt.json','INPUT.json','SELECTOR.json.zlib','PLAN.json.zlib','RESULT.json',
                     'v.rgba.npy','h.rgba.npy','both.rgba.npy','off.rgba.npy'))
BATCH_FILES=frozenset(('bound.json.zlib','phase.json.zlib','datums.jsonl'))


@dataclass(frozen=True,slots=True)
class SelectedLeaseLimits:
    members:int=4096
    artifact_bytes:int=512*1024**2
    receipt_bytes:int=8*1024**2
    chunk_bytes:int=1024**2

    def __post_init__(self):
        for key,cap in dict(members=4096,artifact_bytes=512*1024**2,receipt_bytes=8*1024**2,chunk_bytes=1024**2).items():
            value=getattr(self,key)
            _need(type(value) is int and 0<=value<=cap and (key!='chunk_bytes' or value>0),'INVALID_REQUEST','Selected IO limit only tightens '+key)


def _ordinal(name):
    _need(type(name) is str and name.isascii() and re.fullmatch(r'batch-[0-9]+',name,re.ASCII) is not None,
          'CONFLICT','Unknown selected bundle directory')
    # Native single-component Windows names have an independent small bound.
    _need(len(name)<=255,'CONFLICT','Selected directory component too long')
    ordinal=int(name[6:])
    _need(name==f'batch-{ordinal:04d}','CONFLICT','Noncanonical selected batch ordinal')
    return ordinal


class SelectedWindowsBundleLease(WindowsBundleLease):
    def __init__(self,root,*,expected_root_identity,forbidden_file_identities,reader_api,limits,checkpoint):
        _need(type(limits) is SelectedLeaseLimits,'INVALID_REQUEST','Explicit selected IO limits required')
        super().__init__(root,expected_root_identity=expected_root_identity,
                         forbidden_file_identities=forbidden_file_identities,reader_api=reader_api,
                         limits=limits,checkpoint=checkpoint)

    def _scan(self,acquire_pages=False):
        self._point();found={};entry_count=0;root_files=set();batch_names=[];folded=set()
        enumeration_check=_CheckpointCall(self._point)
        h=directories.open_root(self._root)
        try:
            self._same_directory(self._dir_guards[''],h)
            for name,attributes,size in directories.entries(h,enumeration_check):
                self._point();entry_count+=1
                _need(entry_count<=2*self._limits['members'],'RESOURCE_LIMIT','Selected enumeration entry limit')
                _need(type(name) is str and name.isascii() and name.casefold() not in folded,
                      'CONFLICT','Noncanonical/case-aliased selected member')
                folded.add(name.casefold())
                _need(not attributes&0x400,'CONFLICT','Selected reparse entry forbidden')
                if attributes&0x10:
                    _ordinal(name);batch_names.append(name)
                    _need(len(batch_names)<=self._limits['members'],'RESOURCE_LIMIT','Selected directory limit')
                else:
                    _need(name in ROOT_FILES,'CONFLICT','Unknown selected root file')
                    root_files.add(name);found[name]=(attributes,size)
            _need(root_files==set(ROOT_FILES),'CONFLICT','Selected required root files missing')
            batch_names.sort(key=_ordinal)
            _need(batch_names==[f'batch-{i:04d}' for i in range(len(batch_names))],
                  'CONFLICT','Selected batches must be continuous from zero')
            if acquire_pages:
                _need(self._state=='opening' and set(self._dir_guards)=={''},'CONFLICT','Selected directory capture must be fresh')
                for name in batch_names:
                    self._point();guard=self._stack.enter_context(FileGuard(self._root/name,directory=True))
                    self._directory_meta(guard.handle);self._dir_guards[name]=guard
            _need(set(self._dir_guards)=={''}|set(batch_names),'CONFLICT','Selected directory membership changed')
            for batch in batch_names:
                self._point();child=directories.open_child(h,batch)
                _need(child is not None,'CONFLICT','Selected batch directory disappeared')
                try:
                    self._same_directory(self._dir_guards[batch],child)
                    child_names=set();child_folded=set()
                    for name,attributes,size in directories.entries(child,enumeration_check):
                        self._point();entry_count+=1
                        _need(entry_count<=2*self._limits['members'],'RESOURCE_LIMIT','Selected enumeration entry limit')
                        _need(type(name) is str and name.isascii() and name.casefold() not in child_folded,
                              'CONFLICT','Noncanonical/case-aliased selected batch file')
                        child_folded.add(name.casefold())
                        _need(not attributes&(0x400|0x10),'CONFLICT','Nested directory/reparse selected entry forbidden')
                        _need(name in BATCH_FILES,'CONFLICT','Unknown selected batch file')
                        child_names.add(name);found[batch+'/'+name]=(attributes,size)
                    _need(child_names==set(BATCH_FILES),'CONFLICT','Selected batch files missing')
                finally:kernel().CloseHandle(child)
        except BaseException:
            if enumeration_check.error is not None:raise enumeration_check.error
            raise
        finally:kernel().CloseHandle(h)
        _need(len(found)<=self._limits['members'],'RESOURCE_LIMIT','Selected file limit')
        return found

    def inventory(self):
        with self._lock:
            self._open_required();self._point()
            _need(self._scan()==self._captured_entries,'CONFLICT','Selected membership/metadata changed')
            for path,guard,identity in self._ancestors:
                self._point();_need(self._directory_meta(guard.handle).identity==identity,'CONFLICT','Held selected ancestor changed')
                with FileGuard(path,directory=True) as fresh:
                    _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Selected ancestor path changed')
            batches=sorted((name for name in self._dir_guards if name),key=_ordinal)
            for name in batches:
                self._point();guard=self._dir_guards[name];identity=self._directory_meta(guard.handle).identity
                with FileGuard(self._root/name,directory=True) as fresh:
                    _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Selected batch path changed')
            values=[]
            for name,guard in sorted(self._files.items()):
                self._point();current=self._meta(guard.handle)
                _need(current==self._metadata[name],'CONFLICT','Held selected member metadata changed')
                with FileGuard(self._root/Path(name)) as fresh:
                    _need(self._meta(fresh.handle)==current,'CONFLICT','Selected member path identity/metadata changed')
                values.append(self._MemberInfo(relative_name=name,size=current.size,volume=current.volume,
                                              file_index=current.file_index,link_count=current.link_count))
            self._point()
            return self._Inventory(files=tuple(values),directories=tuple(batches))
