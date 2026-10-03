# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows file identity, deny-write guards, and rename without replacing a destination."""
import ctypes
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path

from protocol import Failure


def kernel():
    if os.name != "nt":
        raise Failure("UNSUPPORTED", "Transactions currently require Windows")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "ReadFile": ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
        "SetFilePointerEx": ([w.HANDLE, ctypes.c_int64, ctypes.c_void_p, w.DWORD], w.BOOL),
        "GetFileInformationByHandle": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
        "SetFileInformationByHandle": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "CreateMutexW": ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "ReleaseMutex": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(k, name).argtypes = args
        getattr(k, name).restype = result
    return k


def native_path(path):
    text = os.path.abspath(path)  # Never follow a final-component symlink before opening.
    if text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


def fail_io(label):
    number = ctypes.get_last_error()
    code = "CONFLICT" if number in (5, 32, 33, 80, 183) else "NOT_FOUND" if number in (2, 3) else "IO_ERROR"
    raise Failure(code, f"{label}: {ctypes.WinError(number)}")


def read_shared_bytes(path):
    """Read an atomic JSON generation without excluding the writer's rename handle.

    This is a status reader, NOT a transaction content guard. Share DELETE allows
    the writer to replace the path while this reader finishes the old generation.
    """
    import msvcrt
    k = kernel()
    handle = k.CreateFileW(native_path(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        k.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


class FileGuard:
    """Hold actual file open; hash and rename through that SAME handle (no TOCTOU gap)."""
    def __init__(self, path, *, renameable=False, directory=False):
        self.path = Path(os.path.abspath(path))
        self.k = kernel()
        self.renameable = renameable
        access = 0 if directory else 0x80000000 | (0x10000 if renameable else 0)
        share = 3 if directory else 1  # Never share DELETE; files never share WRITE.
        self.handle = self.k.CreateFileW(native_path(self.path), access, share, None, 3,
                                         0x00200000 | (0x02000000 if directory else 0x80), None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            fail_io(f"Cannot guard {self.path}")
        try:
            attributes = self._information().attributes
            if attributes & 0x400 or (not directory and attributes & 0x10):
                raise Failure("CONFLICT", "Reparse points and non-regular transaction files are forbidden")
        except BaseException:
            self.close()
            raise

    def _information(self):
        class Info(ctypes.Structure):
            _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME), ("accessed", w.FILETIME),
                        ("written", w.FILETIME), ("volume", w.DWORD), ("size_high", w.DWORD),
                        ("size_low", w.DWORD), ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]
        info = Info()
        if not self.k.GetFileInformationByHandle(self.handle, ctypes.byref(info)):
            fail_io("GetFileInformationByHandle")
        return info

    def identity(self):
        info = self._information()
        return {"volume": info.volume, "file_index": (info.index_high << 32) | info.index_low}

    def chunks(self, checkpoint=lambda: None):
        if not self.k.SetFilePointerEx(self.handle, 0, None, 0):
            fail_io("SetFilePointerEx")
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = w.DWORD()
        while True:
            checkpoint()
            if not self.k.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                fail_io("ReadFile")
            if not count.value:
                break
            yield buffer.raw[:count.value]

    def sha256(self, checkpoint=lambda: None):
        digest = hashlib.sha256()
        for block in self.chunks(checkpoint):
            digest.update(block)
        return digest.hexdigest()

    def rename_new(self, destination):
        if not self.renameable:
            raise ValueError("Guard was not opened for rename")
        destination = Path(os.path.abspath(destination))
        if os.path.lexists(destination):
            raise Failure("CONFLICT", f"Destination already exists: {destination}")
        if self._information().links != 1:
            raise Failure("CONFLICT", "Rename requires an unaliased single-link transaction file")
        encoded = str(destination).encode("utf-16-le")
        class RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", w.DWORD), ("RootDirectory", w.HANDLE),
                        ("FileNameLength", w.DWORD), ("FileName", w.WCHAR * (len(encoded) // 2 + 1))]
        info = RenameInfo()
        info.Flags = 0  # FileRenameInfo: ReplaceIfExists = FALSE
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, encoded, len(encoded))
        if not self.k.SetFileInformationByHandle(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
            fail_io(f"Rename without replacement to {destination}")
        self.path = destination

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PathLocks:
    """Per-canonical-path OS mutexes; crash releases ownership without deleting lock files."""
    def __init__(self, paths):
        self.k = kernel()
        self.handles = []
        self.paths = sorted({os.path.normcase(str(Path(p).resolve())) for p in paths})

    def __enter__(self):
        try:
            for path in self.paths:
                name = "Local\\blenderctl-" + hashlib.sha256(path.encode("utf-8")).hexdigest()
                handle = self.k.CreateMutexW(None, False, name)
                if not handle:
                    fail_io("CreateMutexW")
                acquired = self.k.WaitForSingleObject(handle, 0)
                if acquired not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                    self.k.CloseHandle(handle)
                    raise Failure("CONFLICT", f"Path locked by another transaction: {path}")
                self.handles.append(handle)
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        for handle in reversed(self.handles):
            self.k.ReleaseMutex(handle)
            self.k.CloseHandle(handle)
        self.handles.clear()
