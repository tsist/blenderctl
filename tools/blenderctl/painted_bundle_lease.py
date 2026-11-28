# SPDX-License-Identifier: GPL-3.0-or-later
"""Painted two-level namespace over the existing Windows held-handle lease.

Only scanning/inventory differ. FileGuard, chunks, recheck, exclusivity and
close/cancellation are inherited. The reader validates semantic file names;
no unverified receipt is consulted to discover or authorize a path.
"""
from dataclasses import dataclass
from pathlib import Path
import re
from toon_image_bundle_lease import WindowsBundleLease,_need,_CheckpointCall
from filesystem import FileGuard,kernel
import directory_handles as directories

@dataclass(frozen=True,slots=True)
class PaintedLeaseLimits:
    members:int=4096
    artifact_bytes:int=4*1024**3
    receipt_bytes:int=8*1024**2
    chunk_bytes:int=1024**2
    def __post_init__(self):
        for name,cap in dict(members=4096,artifact_bytes=4*1024**3,receipt_bytes=8*1024**2,chunk_bytes=1024**2).items():
            value=getattr(self,name)
            _need(type(value) is int and 0<=value<=cap and (name!='chunk_bytes' or value>0),
                'INVALID_REQUEST','Painted lease only tightens '+name)

_COMPONENT=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z',re.ASCII)
_LEAF=re.compile(r'.+\.(?:json|jsonl|json\.zlib|npy|u64)\Z',re.ASCII)
_RESERVED={'CON','PRN','AUX','NUL',*('COM'+str(i) for i in range(1,10)),*('LPT'+str(i) for i in range(1,10))}

def _component(name):
    _need(type(name) is str and _COMPONENT.fullmatch(name) is not None and not name.endswith('.') and
        name.split('.')[0].upper() not in _RESERVED,'CONFLICT','Noncanonical painted member component')

class PaintedWindowsBundleLease(WindowsBundleLease):
    """One use; constructor and IO capability remain the established contract.

    Painted members is a conservative joint files+directories enumeration cap.
    Reader 16,384 and lease 4,096 therefore have effective cap <=4,096 entries.
    A directory component is never followed through a receipt-supplied path.
    """
    def __init__(self,root,*,expected_root_identity,forbidden_file_identities,reader_api,limits,checkpoint):
        _need(type(limits) is PaintedLeaseLimits,'INVALID_REQUEST','Explicit painted IO limits required')
        super().__init__(root,expected_root_identity=expected_root_identity,
            forbidden_file_identities=forbidden_file_identities,reader_api=reader_api,limits=limits,checkpoint=checkpoint)

    def _scan(self,acquire_pages=False):
        self._point();found={};observed_dirs={};entry_count=0
        check=_CheckpointCall(self._point)
        root=directories.open_root(self._root)
        def walk(handle,prefix,depth):
            nonlocal entry_count
            self._point();_need(depth<=2,'CONFLICT','Painted directory depth exceeds two')
            identity=self._directory_meta(handle).identity
            if prefix:observed_dirs[prefix]=identity
            else:self._same_directory(self._dir_guards[''],handle)
            folded=set()
            for name,attributes,size in directories.entries(handle,check):
                self._point();entry_count+=1
                _need(entry_count<=self._limits['members'],'RESOURCE_LIMIT','Painted total directory entry cap')
                _component(name)
                _need(name.casefold() not in folded,'CONFLICT','Painted case alias forbidden');folded.add(name.casefold())
                _need(not attributes&0x400,'CONFLICT','Painted reparse entry forbidden')
                relative=(prefix+'/' if prefix else '')+name
                if attributes&0x10:
                    _need(depth<2,'CONFLICT','Painted nested directory beyond two levels')
                    child=directories.open_child(handle,name)
                    _need(child is not None,'CONFLICT','Painted directory disappeared')
                    try:walk(child,relative,depth+1)
                    finally:kernel().CloseHandle(child)
                else:
                    _need(_LEAF.fullmatch(name) is not None,'CONFLICT','Unsupported painted member extension')
                    found[relative]=(attributes,size)
        try:walk(root,'',0)
        except BaseException:
            if check.error is not None:raise check.error
            raise
        finally:kernel().CloseHandle(root)
        self._point()
        _need('receipt.json' in found,'CONFLICT','Painted receipt is required')
        identities=[self._directory_meta(self._dir_guards[''].handle).identity,*observed_dirs.values()]
        _need(len(set(identities))==len(identities),'CONFLICT','Painted directory identities alias')
        # Discover the entire bounded namespace before acquiring lasting child
        # guards. Parent guards are acquired first; no receipt path is opened.
        names=sorted(observed_dirs,key=lambda name:(name.count('/'),name))
        if acquire_pages:
            _need(self._state=='opening' and set(self._dir_guards)=={''},'CONFLICT','Painted capture must be fresh')
            for name in names:
                self._point();guard=self._stack.enter_context(FileGuard(self._root/Path(name),directory=True))
                _need(self._directory_meta(guard.handle).identity==observed_dirs[name],
                    'CONFLICT','Painted child directory changed during capture')
                self._dir_guards[name]=guard
        _need(set(self._dir_guards)=={''}|set(observed_dirs),'CONFLICT','Painted directory membership changed')
        for name in names:
            self._point();_need(self._directory_meta(self._dir_guards[name].handle).identity==observed_dirs[name],
                'CONFLICT','Painted held directory no longer matches path enumeration')
        return found

    def inventory(self):
        with self._lock:
            self._open_required();self._point()
            _need(self._scan()==self._captured_entries,'CONFLICT','Painted membership/metadata changed')
            for path,guard,identity in self._ancestors:
                self._point();_need(self._directory_meta(guard.handle).identity==identity,'CONFLICT','Held painted ancestor changed')
                with FileGuard(path,directory=True) as fresh:
                    _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Painted ancestor path changed')
            names=sorted((name for name in self._dir_guards if name),key=lambda name:(name.count('/'),name))
            for name in names:
                self._point();guard=self._dir_guards[name];identity=self._directory_meta(guard.handle).identity
                with FileGuard(self._root/Path(name),directory=True) as fresh:
                    _need(self._directory_meta(fresh.handle).identity==identity,'CONFLICT','Painted child directory path changed')
            values=[]
            for name,guard in sorted(self._files.items()):
                self._point();current=self._meta(guard.handle)
                _need(current==self._metadata[name],'CONFLICT','Held painted member metadata changed')
                with FileGuard(self._root/Path(name)) as fresh:
                    _need(self._meta(fresh.handle)==current,'CONFLICT','Painted member path identity/metadata changed')
                values.append(self._MemberInfo(relative_name=name,size=current.size,volume=current.volume,
                    file_index=current.file_index,link_count=current.link_count))
            self._point();return self._Inventory(files=tuple(values),directories=tuple(names))
