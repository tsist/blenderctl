"""Attempt-local actual file/ancestor guards; no semantic or SHA cache beyond live handles."""
import os,re
from pathlib import Path
from filesystem import FileGuard
from protocol import Failure,read_json

def descriptor(row):
    if type(row) is not dict or set(row)!={'file','expected_sha256'} or type(row['file']) is not str or not Path(row['file']).is_absolute() or type(row['expected_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',row['expected_sha256']):
        raise Failure('INVALID_REQUEST','M5 requires an absolute file/lower-case SHA descriptor')
    return {**row,'file':os.path.abspath(row['file'])}

class GuardSet:
    def __init__(self,stack):
        self.stack=stack;self.files={};self.parents={}
        self.stats={'initial_full_sha':0,'final_full_sha':0,'file_path_identity_checks':0,'ancestor_path_identity_checks':0}
    def _parents(self,path):
        for parent in reversed(path.parents):
            key=os.path.normcase(str(parent))
            if key not in self.parents:
                guard=self.stack.enter_context(FileGuard(parent,directory=True))
                self.parents[key]={'path':str(parent),'guard':guard,'identity':guard.identity()}
    def add(self,row):
        row=descriptor(row);path=Path(row['file']);key=os.path.normcase(str(path))
        if key in self.files:
            if self.files[key]['descriptor']!=row:raise Failure('CONFLICT','M5 same file has conflicting SHA')
            return self.files[key]['guard']
        if len(self.files)>=8192:raise Failure('UNSUPPORTED','M5 scope exceeds8192 guarded descriptors')
        self._parents(path);guard=self.stack.enter_context(FileGuard(path));actual=guard.sha256();self.stats['initial_full_sha']+=1
        if actual!=row['expected_sha256']:raise Failure('CONFLICT','M5 initial guarded SHA differs: '+str(path))
        self.files[key]={'descriptor':row,'guard':guard,'identity':guard.identity()}
        return guard
    def protect_output_parent(self,path):
        """Hold the already-created owned output namespace before atomic creation."""
        self._parents(Path(os.path.abspath(path)))
    def capture(self,path):
        """Capture generated output or terminal authority bytes under the SAME live handle."""
        path=Path(os.path.abspath(path));key=os.path.normcase(str(path))
        if key not in self.files:
            if len(self.files)>=8192:raise Failure('UNSUPPORTED','M5 scope exceeds8192 guarded descriptors')
            self._parents(path);guard=self.stack.enter_context(FileGuard(path));value=guard.sha256();self.stats['initial_full_sha']+=1
            self.files[key]={'descriptor':{'file':str(path),'expected_sha256':value},'guard':guard,'identity':guard.identity()}
        return dict(self.files[key]['descriptor'])
    def read(self,row):
        row=descriptor(row);self.add(row);return read_json(row['file'])
    def capture_json(self,path):
        row=self.capture(path);return self.read(row),row
    def recheck(self):
        for row in self.parents.values():
            guard=row['guard']
            if not guard.handle or guard.identity()!=row['identity']:raise Failure('CONFLICT','M5 live ancestor identity changed')
            with FileGuard(row['path'],directory=True) as opened:
                if opened.identity()!=row['identity']:raise Failure('CONFLICT','M5 ancestor path identity changed')
            self.stats['ancestor_path_identity_checks']+=1
        for row in self.files.values():
            guard=row['guard'];wanted=row['descriptor']
            if not guard.handle or guard.identity()!=row['identity'] or guard.sha256()!=wanted['expected_sha256']:raise Failure('CONFLICT','M5 final input/closure full SHA or handle identity changed')
            self.stats['final_full_sha']+=1
            with FileGuard(wanted['file']) as opened:
                if opened.identity()!=row['identity']:raise Failure('CONFLICT','M5 input/closure path identity changed')
            self.stats['file_path_identity_checks']+=1
        return {**self.stats,'files':len(self.files),'ancestors':len(self.parents),'status':'passed'}

def scope(stack):
    if not hasattr(stack,'_m5_guard_set'):stack._m5_guard_set=GuardSet(stack)
    return stack._m5_guard_set
