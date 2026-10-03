# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows local-directory sampling using handle-relative, no-reparse opens.

All mutable child names are single components relative to an already opened
directory. Share R/W/DELETE so atomic child replacement remains possible.
Sampling is bounded and non-atomic; it is not a filesystem quota.
"""
import ctypes as c,os,struct,time
from ctypes import wintypes as w
from pathlib import Path
from protocol import Failure
from filesystem import kernel,native_path

P=c.c_void_p
class Unicode(c.Structure):
    _fields_=[('Length',w.USHORT),('MaximumLength',w.USHORT),('Buffer',P)]
class Attributes(c.Structure):
    _fields_=[('Length',w.ULONG),('RootDirectory',w.HANDLE),('ObjectName',c.POINTER(Unicode)),('Attributes',w.ULONG),('SecurityDescriptor',P),('SecurityQualityOfService',P)]
class IO(c.Structure):
    _fields_=[('Status',P),('Information',c.c_size_t)]

def api():
    n=c.WinDLL('ntdll')
    n.NtCreateFile.argtypes=[c.POINTER(w.HANDLE),w.ULONG,c.POINTER(Attributes),c.POINTER(IO),P,w.ULONG,w.ULONG,w.ULONG,w.ULONG,P,w.ULONG];n.NtCreateFile.restype=c.c_long
    n.NtQueryDirectoryFile.argtypes=[w.HANDLE,w.HANDLE,P,P,c.POINTER(IO),P,w.ULONG,c.c_int,c.c_ubyte,P,c.c_ubyte];n.NtQueryDirectoryFile.restype=c.c_long
    return n

def directory_info(handle):
    # FileAttributeTagInfo, queried from the opened object, never its old name.
    k=kernel();k.GetFileInformationByHandleEx.argtypes=[w.HANDLE,c.c_int,P,w.DWORD];k.GetFileInformationByHandleEx.restype=w.BOOL
    info=(w.DWORD*2)()
    if not k.GetFileInformationByHandleEx(handle,9,c.byref(info),c.sizeof(info)):raise c.WinError(c.get_last_error())
    if info[0]&0x400:raise Failure('VALIDATION_FAILED','Directory reparse point rejected by handle')
    if not info[0]&0x10:raise Failure('VALIDATION_FAILED','Expected directory handle')

def open_child(parent,name):
    if not name or name in ('.','..') or any(x in name for x in ('\\','/',':','\0')):
        raise Failure('VALIDATION_FAILED','Invalid directory component')
    raw=c.create_unicode_buffer(name);length=len(name.encode('utf-16-le'))
    if length>65532:raise Failure('VALIDATION_FAILED','Directory name too long')
    u=Unicode(length,length+2,c.cast(raw,P));a=Attributes(c.sizeof(Attributes),parent,c.pointer(u),0x40,None,None);io=IO();h=w.HANDLE()
    # SYNCHRONIZE|LIST_DIRECTORY|READ_ATTRIBUTES, FILE_OPEN, DIRECTORY_FILE|
    # OPEN_REPARSE_POINT|SYNCHRONOUS_IO_NONALERT. No intermediate components.
    status=api().NtCreateFile(c.byref(h),0x100081,c.byref(a),c.byref(io),None,0,7,1,0x200021,None,0)&0xffffffff
    if status in (0xc0000034,0xc000003a,0xc0000056,0xc0000103):return None
    if status:raise Failure('VALIDATION_FAILED',f'Cannot safely open directory component: NTSTATUS {status:#x}')
    try:directory_info(h)
    except BaseException:kernel().CloseHandle(h);raise
    return h.value

def open_root(path):
    p=Path(os.path.abspath(path))
    if os.name!='nt' or not p.drive or str(p).startswith('\\\\'):
        raise Failure('UNSUPPORTED','Handle directory sampling requires local Windows drive')
    k=kernel();h=k.CreateFileW(native_path(p.anchor),0x100081,7,None,3,0x02200000,None)
    if h==P(-1).value:raise c.WinError(c.get_last_error())
    try:
        directory_info(h)
        for name in p.parts[1:]:
            child=open_child(h,name)
            if child is None:raise Failure('NOT_FOUND','Sampling root disappeared')
            k.CloseHandle(h);h=child
        return h
    except BaseException:k.CloseHandle(h);raise

def entries(handle,check):
    buffer=c.create_string_buffer(65536);io=IO();first=True;n=api()
    while True:
        check();status=n.NtQueryDirectoryFile(handle,None,None,None,c.byref(io),buffer,len(buffer),1,False,None,first)&0xffffffff;first=False
        if status in (0x80000006,0xc0000056):return
        if status or not io.Information or io.Information>len(buffer):raise Failure('VALIDATION_FAILED',f'Directory enumeration failed: {status:#x}')
        data=buffer.raw[:io.Information];offset=0
        while True:
            if offset+64>len(data):raise Failure('VALIDATION_FAILED','Truncated directory record')
            next_offset=struct.unpack_from('<I',data,offset)[0];size=struct.unpack_from('<q',data,offset+40)[0]
            attributes,length=struct.unpack_from('<II',data,offset+56)
            if length%2 or offset+64+length>len(data) or size<0:raise Failure('VALIDATION_FAILED','Invalid directory record')
            name=data[offset+64:offset+64+length].decode('utf-16-le')
            if name not in ('.','..'):yield name,attributes,size
            if not next_offset:break
            if next_offset<64+length or next_offset%8:raise Failure('VALIDATION_FAILED','Invalid directory record offset')
            offset+=next_offset

def disk_usage(root,*,max_entries=100000,max_seconds=10,checkpoint=lambda:None):
    total=0;count=0;deadline=time.monotonic()+max_seconds;k=kernel()
    def check():
        checkpoint()
        if time.monotonic()>deadline:raise Failure('RESOURCE_LIMIT','Directory sampling time budget exceeded')
    def walk(handle,depth):
        nonlocal total,count
        if depth>128:raise Failure('RESOURCE_LIMIT','Directory sampling depth exceeded')
        for name,attributes,size in entries(handle,check):
            count+=1
            if count>max_entries:raise Failure('RESOURCE_LIMIT','Directory sampling entry budget exceeded')
            if attributes&0x400:raise Failure('VALIDATION_FAILED','Reparse entry rejected during directory sampling')
            if attributes&0x10:
                child=open_child(handle,name)
                if child is not None:
                    try:walk(child,depth+1)
                    finally:k.CloseHandle(child)
            else:total+=size
    h=open_root(root)
    try:walk(h,0);return total
    finally:k.CloseHandle(h)
