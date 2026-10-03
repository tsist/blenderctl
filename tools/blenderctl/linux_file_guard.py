# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed, *read-only* guards for qualified local Linux filesystems.

This is not Windows deny-delete sharing. A kernel read lease excludes existing
writers (including writable mappings) and blocks new write opens until its
break timeout. F_GETLEASE reports a pending break immediately; a break is a
permanent conflict, even if no bytes changed. Inotify and pinned directory
identities detect namespace changes; they do not prevent rename or unlink.
Call check_all_guards() at supervisor checkpoints and before accepting results,
and handle Failure from close()/a clean context exit. Consumers must finish
before that final validation; these guards cannot authorize publishing writes.

Only local ext2/3/4, XFS, Btrfs and tmpfs namespaces are qualified. Overlay,
network, FUSE and unknown filesystems fail closed, even if F_SETLEASE happens
to succeed. Kernel/admin attacks, changing mounts/namespaces, raw-device writes
and hostile code inside this process are outside this boundary. No sysctl,
permission, signal disposition or system-wide setting is changed.
"""
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import stat
import struct
import sys
import threading
import weakref

from protocol import Failure

# Linux UAPI values, also used on Python versions predating their fcntl exports.
_F_SETOWN_EX = getattr(fcntl, 'F_SETOWN_EX', 15)
_F_GETOWN_EX = getattr(fcntl, 'F_GETOWN_EX', 16)
_F_OWNER_TID = 0
_LOCAL_FILESYSTEMS = {0xEF53: 'ext2/3/4', 0x58465342: 'XFS',
                      0x9123683E: 'Btrfs', 0x01021994: 'tmpfs'}
_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ONLYDIR = 0x01000000
_SELF_EVENTS = _IN_ATTRIB | _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT | _IN_IGNORED
_WATCH_EVENTS = (_SELF_EVENTS | _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVED_FROM |
                 _IN_MOVED_TO | _IN_CREATE | _IN_DELETE)
_EVENT = struct.Struct('iIII')
_registry = weakref.WeakSet()
_registry_lock = threading.RLock()
_sink_lock = threading.RLock()
_sink = None


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
        libc.fstatfs.restype = ctypes.c_int
    except AttributeError as exc:
        raise Failure('UNSUPPORTED', 'Linux guards require inotify and fstatfs') from exc
    return libc


def _filesystem_type(libc, fd):
    # The native Linux statfs structure starts with a long f_type. This buffer
    # exceeds the complete structure on the qualified 64-bit Linux ABIs.
    info = ctypes.create_string_buffer(256)
    if libc.fstatfs(fd, info) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return ctypes.c_ulong.from_buffer(info).value


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _fingerprint(info):
    return (_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_nlink, info.st_mode, info.st_uid, info.st_gid)


def _io_failure(exc, path, *, lease=False):
    if exc.errno in (errno.EAGAIN, errno.ELOOP, errno.ENOTDIR):
        code = 'CONFLICT'
    elif lease or exc.errno in (errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOLCK, errno.EINVAL):
        code = 'UNSUPPORTED'
    elif exc.errno == errno.ENOENT:
        code = 'NOT_FOUND'
    else:
        code = 'IO_ERROR'
    return Failure(code, f'Cannot guard {path}: {exc}')


class _SignalSink:
    """Receive thread-directed, blocked SIGIO without touching global handlers.

    Lease setup preserves an explicitly assigned F_SETOWN_EX owner. A private
    thread blocks SIGIO before publishing its TID, and lives until the last
    lease FD has been unlocked/closed. Notifications may coalesce: they are not
    our source of truth, F_GETLEASE is. Pending signals disappear when this
    thread exits, rather than leaking to Python or Blender's main thread.
    """
    def __init__(self):
        self.pid = os.getpid()
        self.references = 0
        self.tid = None
        self.error = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='blenderctl-lease-signals', daemon=True)
        self.thread.start()
        self.ready.wait()
        if self.error is not None:
            self.thread.join()
            raise Failure('UNSUPPORTED', f'Cannot isolate Linux lease signals: {self.error}')

    def _run(self):
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGIO})
            self.tid = threading.get_native_id()
        except BaseException as exc:
            self.error = exc
        finally:
            self.ready.set()
        if self.error is None:
            self.stop.wait()


def _acquire_sink():
    global _sink
    with _sink_lock:
        if _sink is None:
            _sink = _SignalSink()
        if _sink.pid != os.getpid() or not _sink.thread.is_alive():
            raise Failure('UNSUPPORTED', 'Linux lease signal owner is unavailable')
        _sink.references += 1
        return _sink


def _release_sink(sink):
    global _sink
    if sink is None or sink.pid != os.getpid():
        return
    with _sink_lock:
        sink.references -= 1
        if sink.references == 0:
            sink.stop.set()
            sink.thread.join()
            if _sink is sink:
                _sink = None


def check_all_guards():
    """Validate all live guards in this process, without consuming conflicts."""
    with _registry_lock:
        guards = list(_registry)
    for guard in guards:
        guard.check()


class LinuxFileGuard:
    def __init__(self, path, *, renameable=False, directory=False):
        self.handle = None
        self._notify_fd = None
        self._directories = []
        self._watches = {}
        self._signal_sink = None
        self._failure = None
        self._ready = False
        self._lease_acquired = False
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self.renameable = False
        self.path = Path(os.path.abspath(path))
        if renameable or directory:
            raise Failure('UNSUPPORTED', 'Linux transaction rename and directory guards are not implemented')
        if '..' in os.fspath(path).split(os.sep):
            raise Failure('CONFLICT', 'Linux guarded paths may not contain parent traversal')
        if (sys.platform != 'linux' or ctypes.sizeof(ctypes.c_long) != 8 or
                os.uname().machine not in ('x86_64', 'aarch64') or
                not hasattr(signal, 'pthread_sigmask')):
            raise Failure('UNSUPPORTED', 'Linux guards require 64-bit x86_64/aarch64 Linux with pthread_sigmask')
        self._lib = _libc()
        try:
            self._notify_fd = self._lib.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if self._notify_fd < 0:
                self._notify_fd = None
                number = ctypes.get_errno()
                raise OSError(number, os.strerror(number))
            components = self.path.parts[1:]
            if not components:
                raise Failure('CONFLICT', 'Linux read guards require a regular file')
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            root_fd = os.open('/', flags)
            self._directories.append((root_fd, None, None))
            self._directories[-1] = (root_fd, None, os.fstat(root_fd))
            self._require_filesystem(root_fd)
            self._watch(root_fd, components[0])
            parent_fd = root_fd
            for offset, name in enumerate(components[:-1]):
                fd = os.open(name, flags, dir_fd=parent_fd)
                self._directories.append((fd, name, None))
                self._directories[-1] = (fd, name, os.fstat(fd))
                self._require_filesystem(fd)
                self._watch(fd, components[offset + 1])
                parent_fd = fd
            self.handle = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW |
                                  os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent_fd)
            info = os.fstat(self.handle)
            if not stat.S_ISREG(info.st_mode):
                raise Failure('CONFLICT', 'Linux read guards require a regular file')
            self._require_filesystem(self.handle)
            self._initial = info
            self._watch(self.handle)
            self._signal_sink = _acquire_sink()
            owner = struct.pack('ii', _F_OWNER_TID, self._signal_sink.tid)
            try:
                # Set the owner BEFORE acquiring a lease: there is no interval
                # in which a competing writer could send SIGIO to this process.
                fcntl.fcntl(self.handle, _F_SETOWN_EX, owner)
                fcntl.fcntl(self.handle, fcntl.F_SETLEASE, fcntl.F_RDLCK)
                self._lease_acquired = True
                if fcntl.fcntl(self.handle, _F_GETOWN_EX, bytes(8)) != owner:
                    raise Failure('UNSUPPORTED', 'Kernel did not retain the private lease signal owner')
            except OSError as exc:
                raise _io_failure(exc, self.path, lease=True) from exc
            self._ready = True
            self.check()
            with _registry_lock:
                _registry.add(self)
        except BaseException as exc:
            self.close(validate=False)
            if isinstance(exc, OSError):
                raise _io_failure(exc, self.path) from exc
            raise

    def _require_filesystem(self, fd):
        kind = _filesystem_type(self._lib, fd)
        if kind not in _LOCAL_FILESYSTEMS:
            raise Failure('UNSUPPORTED', f'Linux guarded paths require local ext2/3/4, XFS, Btrfs or tmpfs; '
                          f'filesystem 0x{kind:x} is not qualified: {self.path}')

    def _watch(self, fd, child=None):
        # /proc/self/fd pins the exact already-open inode, unlike re-resolving
        # its pathname during watch installation. User symlinks are forbidden.
        mask = _WATCH_EVENTS | (_IN_ONLYDIR if child is not None else 0)
        wd = self._lib.inotify_add_watch(self._notify_fd, os.fsencode(f'/proc/self/fd/{fd}'), mask)
        if wd < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        names = self._watches.setdefault(wd, set())
        names.add(os.fsencode(child) if child is not None else None)

    def _conflict(self, message):
        if self._failure is None:
            self._failure = Failure('CONFLICT', f'{message}: {self.path}')
        raise self._failure

    def _events(self):
        # Bound work under continuous unrelated directory traffic. An overflow
        # or an unbounded backlog invalidates the guard, never a silent reset.
        for _ in range(64):
            try:
                data = os.read(self._notify_fd, 65536)
            except BlockingIOError:
                return
            if not data:
                self._conflict('Linux namespace monitor ended')
            offset = 0
            while offset < len(data):
                if len(data) - offset < _EVENT.size:
                    self._conflict('Truncated Linux namespace event')
                wd, mask, cookie, length = _EVENT.unpack_from(data, offset)
                offset += _EVENT.size
                if offset + length > len(data):
                    self._conflict('Truncated Linux namespace event name')
                name = data[offset:offset + length].split(b'\0', 1)[0]
                offset += length
                if mask & _IN_Q_OVERFLOW or wd not in self._watches:
                    self._conflict('Linux namespace monitoring lost events')
                names = self._watches[wd]
                if (None in names or (not name and mask & _SELF_EVENTS) or name in names):
                    self._conflict('Guarded file or ancestor namespace changed')
        self._conflict('Linux namespace event backlog exceeded validation bound')

    def check(self):
        with self._lock:
            if self._pid != os.getpid():
                raise Failure('UNSUPPORTED', 'An inherited Linux file guard cannot be used after fork')
            if self._failure is not None:
                raise self._failure
            if not self._ready or self.handle is None:
                raise Failure('CONFLICT', 'Linux file guard is closed or not initialized')
            try:
                self._events()
                sink = self._signal_sink
                if sink is None or not sink.thread.is_alive():
                    self._conflict('Linux lease signal owner ended')
                owner = struct.pack('ii', _F_OWNER_TID, sink.tid)
                if fcntl.fcntl(self.handle, _F_GETOWN_EX, bytes(8)) != owner:
                    self._conflict('Linux lease signal owner changed')
                if fcntl.fcntl(self.handle, fcntl.F_GETLEASE) != fcntl.F_RDLCK:
                    self._conflict('Linux read lease was broken or a writer requested access')
                if _fingerprint(os.fstat(self.handle)) != _fingerprint(self._initial):
                    self._conflict('Guarded file metadata changed')
                parent_fd = None
                for fd, name, initial in self._directories:
                    actual = os.stat('/', follow_symlinks=False) if parent_fd is None else os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False)
                    if _identity(actual) != _identity(initial) or _identity(os.fstat(fd)) != _identity(initial):
                        self._conflict('Guarded ancestor identity changed')
                    parent_fd = fd
                actual = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if _fingerprint(actual) != _fingerprint(self._initial):
                    self._conflict('Guarded pathname no longer identifies the original file')
                self._events()
                # Lease breaks can race with the path walk, so check last too.
                if fcntl.fcntl(self.handle, fcntl.F_GETLEASE) != fcntl.F_RDLCK:
                    self._conflict('Linux read lease was broken during validation')
            except OSError as exc:
                self._conflict(f'Linux guard validation failed ({exc})')
        return self

    def identity(self):
        self.check()
        return {'volume': self._initial.st_dev, 'file_index': self._initial.st_ino}

    def chunks(self, checkpoint=lambda: None):
        offset = 0
        while True:
            checkpoint()
            self.check()
            block = os.pread(self.handle, 1024 * 1024, offset)
            self.check()
            if not block:
                return
            offset += len(block)
            yield block

    def sha256(self, checkpoint=lambda: None):
        digest = hashlib.sha256()
        for block in self.chunks(checkpoint):
            digest.update(block)
        self.check()
        return digest.hexdigest()

    def rename_new(self, destination):
        raise Failure('UNSUPPORTED', 'Linux transaction rename is not implemented')

    def _close_fds(self, *, unlock):
        first_error = None
        if self.handle is not None:
            fd, self.handle = self.handle, None
            if unlock and self._lease_acquired:
                try:
                    fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
                except OSError as exc:
                    # No lease after failed acquisition or forced expiry.
                    if exc.errno not in (errno.EAGAIN, errno.EINVAL):
                        first_error = exc
            self._lease_acquired = False
            try:
                os.close(fd)
            except OSError as exc:
                first_error = first_error or exc
        fds = [row[0] for row in self._directories]
        self._directories.clear()
        if self._notify_fd is not None:
            fds.append(self._notify_fd)
            self._notify_fd = None
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def close(self, *, validate=True):
        with self._lock:
            try:
                if validate and self._ready and self.handle is not None:
                    self.check()
            finally:
                self._ready = False
                with _registry_lock:
                    _registry.discard(self)
                try:
                    self._close_fds(unlock=self._pid == os.getpid())
                except OSError as exc:
                    raise Failure('IO_ERROR', f'Linux guard cleanup failed: {exc}') from exc
                finally:
                    sink, self._signal_sink = self._signal_sink, None
                    _release_sink(sink)

    def __enter__(self):
        return self.check()

    def __exit__(self, exc_type, exc, traceback):
        self.close(validate=exc_type is None)

    def __del__(self):
        try:
            self.close(validate=False)
        except BaseException:
            pass


def _after_fork():
    # fork+exec is safe via CLOEXEC. For plain fork, discard child copies without
    # F_UNLCK (which would revoke the parent's shared open-file-description
    # lease). Threads/locks are not inherited as usable synchronization objects.
    global _registry, _registry_lock, _sink, _sink_lock
    guards = list(_registry)
    _registry = weakref.WeakSet()
    _registry_lock = threading.RLock()
    _sink = None
    _sink_lock = threading.RLock()
    for guard in guards:
        guard._lock = threading.RLock()
        guard._signal_sink = None
        guard._ready = False
        try:
            guard._close_fds(unlock=False)
        except OSError:
            pass


os.register_at_fork(after_in_child=_after_fork)
