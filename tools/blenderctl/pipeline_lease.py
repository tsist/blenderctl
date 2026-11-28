"""Short-lived Windows runtime leases for public pipeline jobs."""

import ctypes
from ctypes import wintypes
import json
import math
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener, answer_challenge, deliver_challenge
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack

from filesystem import FileGuard, PathLocks, kernel, native_path
from protocol import Failure, atomic_json, read_json


WAIT_TIMEOUT = 258
PIPE_ERRORS = (OSError, EOFError, AuthenticationError, ValueError)
RPC_TIMEOUT = 2.0


class _DataBlob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(wintypes.BYTE))]


def _user_token(data, *, decrypt=False):
    """Keep the pipe credential private to the current Windows user."""
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    change = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    change.argtypes = (ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob))
    change.restype = wintypes.BOOL
    buffer = (wintypes.BYTE * len(data)).from_buffer_copy(data)
    source, output = _DataBlob(len(data), buffer), _DataBlob()
    if not change(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = kernel().LocalFree
    free.argtypes = (wintypes.HLOCAL,)
    free.restype = wintypes.HLOCAL
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free(ctypes.cast(output.data, wintypes.HLOCAL))


class _Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                ('event', wintypes.HANDLE)]


def _runtime_member_changed(relative):
    from installation import included_runtime_dir, skipped_runtime_entry
    parts = Path(relative).parts
    if len(parts) <= 1:
        return True
    return included_runtime_dir(parts[0]) and not any(skipped_runtime_entry(part) for part in parts[1:])


def _core_member_changed(relative):
    name = relative.casefold()
    return name.endswith('.py') or name == 'registry.json'


class DirectoryWatch:
    """Track names in the exact inventory and permanently reject an overflow."""

    def __init__(self, path, recursive, relevant):
        self.kernel = kernel()
        self.recursive = recursive
        self.relevant = relevant
        self.lock = threading.Lock()
        self.changed = None
        self.buffer = ctypes.create_string_buffer(65536)
        self.handle = self.event = None
        self.pending = False
        handles = wintypes.HANDLE
        word = wintypes.DWORD
        boolean = wintypes.BOOL
        pointer = ctypes.c_void_p
        signatures = {
            'CreateEventW': ((pointer, boolean, boolean, wintypes.LPCWSTR), handles),
            'ResetEvent': ((handles,), boolean),
            'ReadDirectoryChangesW': ((handles, pointer, word, boolean, word, pointer, pointer, pointer), boolean),
            'CancelIoEx': ((handles, pointer), boolean),
            'GetOverlappedResult': ((handles, pointer, pointer, boolean), boolean),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        try:
            self.event = self.kernel.CreateEventW(None, True, False, None)
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            handle = self.kernel.CreateFileW(native_path(path), 1, 7, None, 3, 0x42000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
            self._arm()
        except OSError as exc:
            self.close()
            raise Failure('UNSUPPORTED', f'Cannot watch runtime directory {path}: {exc}') from exc

    def _arm(self):
        self.kernel.ResetEvent(self.event)
        self.operation = _Overlapped(event=self.event)
        # Names, attributes, size, last write and security. Last-access time
        # can change during a legitimate read and is outside the identity.
        accepted = self.kernel.ReadDirectoryChangesW(self.handle, self.buffer, len(self.buffer), self.recursive,
                                                     0x11F, None, ctypes.byref(self.operation), None)
        if not accepted:
            raise ctypes.WinError(ctypes.get_last_error())
        self.pending = True

    def _consume(self):
        size = wintypes.DWORD()
        succeeded = self.kernel.GetOverlappedResult(self.handle, ctypes.byref(self.operation), ctypes.byref(size), False)
        self.pending = False
        if not succeeded or not 0 < size.value <= len(self.buffer):
            self.changed = {'reason': 'notification_overflow_or_error'}
            return
        offset = 0
        while offset + 12 <= size.value:
            advance, action, length = struct.unpack_from('<III', self.buffer, offset)
            end = offset + 12 + length
            if not length or length % 2 or end > size.value or (advance and offset + advance < end):
                break
            try:
                name = bytes(self.buffer[offset + 12:end]).decode('utf-16-le')
            except UnicodeError:
                break
            if self.relevant(name):
                self.changed = {'reason': 'runtime_member_changed', 'action': action, 'file': name}
                return
            if not advance:
                return
            offset += advance
        self.changed = {'reason': 'invalid_notification_record'}

    def unchanged(self):
        with self.lock:
            if self.changed is not None:
                return False
            for _ in range(32):
                ready = self.kernel.WaitForSingleObject(self.event, 0)
                if ready == WAIT_TIMEOUT:
                    return True
                if ready != 0:
                    break
                self._consume()
                if self.changed is not None:
                    return False
                try:
                    self._arm()
                except OSError:
                    break
            self.changed = {'reason': 'notification_unavailable'}
            return False

    def close(self):
        if self.handle is not None:
            if self.pending:
                self.kernel.CancelIoEx(self.handle, ctypes.byref(self.operation))
                size = wintypes.DWORD()
                self.kernel.GetOverlappedResult(self.handle, ctypes.byref(self.operation), ctypes.byref(size), True)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            self.kernel.CloseHandle(self.event)
            self.event = None


def _address(identity):
    return rf'\\.\pipe\blenderctl-runtime-{identity}'


def _root(jobs_root):
    return Path(jobs_root).absolute() / '.pipeline-runtime-lease'


def _same_path(first, second):
    # Alternate spellings that need disk alias resolution can miss safely.
    if not isinstance(first, (str, os.PathLike)):
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _current(root):
    try:
        identity = read_json(root / 'current.json')['id']
        if uuid.UUID(hex=identity).hex != identity:
            return None
        return root / identity
    except (Failure, KeyError, TypeError, *PIPE_ERRORS):
        return None


def _state(root):
    directory = _current(root)
    if directory is None:
        return None
    try:
        for name in ('closed.json', 'draining.json', 'ready.json', 'starting.json', 'state.json'):
            try:
                state = read_json(directory / name)
            except Failure as exc:
                if exc.code != 'NOT_FOUND':
                    raise
                continue
            return (directory, state) if state['id'] == directory.name else None
        state = read_json(directory / 'spec.json')
        state['pid'] = read_json(directory / 'launch.json')['pid']
        return (directory, state) if state['id'] == directory.name else None
    except (Failure, KeyError, TypeError, *PIPE_ERRORS):
        return None


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    k = kernel()
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    handle = k.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k.CloseHandle(handle)


def _public(state, directory):
    result = {name: state.get(name) for name in (
        'id', 'state', 'pid', 'blender', 'idle_seconds', 'created_at', 'ready_at',
        'closed_at', 'cold_seconds', 'runtime_files', 'reason', 'error', 'change',
    ) if name in state}
    if state['state'] in ('starting', 'ready', 'draining') and not _alive(state.get('pid')):
        result.update(state='stopped', reason='process_exited')
    if (directory / 'stop.request').is_file() and result['state'] in ('starting', 'ready'):
        result.update(state='draining', reason='stop_requested')
    result['log'] = str(directory / 'runtime-lease.log')
    return result


def manage(action, jobs_root, blender, idle_seconds=120, timeout=360):
    """Start or inspect a lease scoped to the selected jobs root."""
    if os.name != 'nt':
        raise Failure('UNSUPPORTED', 'Pipeline runtime leases require Windows')
    root = _root(jobs_root)
    if action in ('status', 'stop'):
        existing = _state(root)
        if existing is None:
            return {'state': 'stopped', 'reason': 'no_lease'}
        directory, state = existing
        result = _public(state, directory)
        if action == 'stop' and result['state'] in ('starting', 'ready', 'draining'):
            (directory / 'stop.request').touch(exist_ok=True)
            result.update(state='draining', reason='stop_requested')
        return result
    if action != 'start' or not 1 <= idle_seconds <= 3600 or not 1 <= timeout <= 3600:
        raise Failure('INVALID_REQUEST', 'Lease start needs an idle and startup timeout of 1..3600s')
    root.mkdir(parents=True, exist_ok=True)
    with PathLocks([root / 'lease-start']):
        existing = _state(root)
        if existing is not None:
            prior = _public(existing[1], existing[0])
            if prior['state'] in ('starting', 'ready', 'draining'):
                if prior['blender'] != str(Path(blender).resolve()):
                    raise Failure('CONFLICT', 'Stop the lease for the other Blender executable first')
                return prior
        identity = uuid.uuid4().hex
        directory = root / identity
        directory.mkdir()
        spec = {'id': identity, 'state': 'starting', 'blender': str(Path(blender).resolve()),
                'core': str(Path(__file__).resolve().parent),
                'python': str(Path(sys.executable).resolve()), 'auth': _user_token(secrets.token_bytes(32)).hex(),
                'idle_seconds': idle_seconds, 'startup_timeout': timeout, 'created_at': time.time()}
        atomic_json(directory / 'spec.json', spec)
        with (directory / 'runtime-lease.log').open('wb') as log:
            process = subprocess.Popen(
                [sys.executable, '-B', __file__, str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        # Publish a complete launch receipt before clients discover the generation.
        atomic_json(directory / 'launch.json', {'pid': process.pid})
        atomic_json(root / 'current.json', {'id': identity})
        return _public({**spec, 'pid': process.pid}, directory)


def _send(pipe, message):
    pipe.send_bytes(json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))


def _receive(pipe, *, max_bytes):
    return json.loads(pipe.recv_bytes(maxlength=max_bytes).decode('utf-8'))


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('Runtime lease RPC deadline expired')
    return seconds


def _connect(address, deadline):
    import _winapi
    from multiprocessing.connection import PipeConnection

    while True:
        try:
            _winapi.WaitNamedPipe(address, math.ceil(_remaining(deadline) * 1000))
            handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                                        _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED,
                                        _winapi.NULL)
            break
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (_winapi.ERROR_SEM_TIMEOUT, _winapi.ERROR_PIPE_BUSY):
                raise
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        return PipeConnection(handle)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


class _TimedPipe:
    """Bound both halves of CPython's existing authenticated pipe protocol."""

    def __init__(self, pipe, deadline):
        self.pipe = pipe
        self.deadline = deadline

    def recv_bytes(self, maxlength):
        if not self.pipe.poll(_remaining(self.deadline)):
            raise TimeoutError('Runtime lease did not reply')
        return self.pipe.recv_bytes(maxlength)

    def send_bytes(self, data):
        import _winapi

        milliseconds = math.ceil(_remaining(self.deadline) * 1000)
        operation, code = _winapi.WriteFile(self.pipe.fileno(), data, overlapped=True)
        if code == _winapi.ERROR_IO_PENDING and _winapi.WaitForSingleObject(operation.event, milliseconds) != 0:
            operation.cancel()
            operation.GetOverlappedResult(True)  # The local pipe driver's cancellation releases the buffer.
            raise TimeoutError('Runtime lease did not consume a request')
        count, code = operation.GetOverlappedResult(False)
        if code or count != len(data):
            raise OSError('Runtime lease pipe write failed')


class Lease:
    def __init__(self, pipe, directory, metadata):
        self.pipe = pipe
        self.id = metadata['id']
        self.runtime_key = metadata['runtime_key']
        self.registry_sha256 = metadata['registry_sha256']
        self.snapshot = directory / 'snapshot.json'
        self.snapshot_sha256 = metadata['snapshot_sha256']
        self._runtime = None

    @property
    def runtime(self):
        if self._runtime is None:
            with FileGuard(self.snapshot) as guard:
                if guard.sha256() != self.snapshot_sha256:
                    raise Failure('CONFLICT', 'Runtime lease snapshot changed')
                self._runtime = read_json(self.snapshot)
        return self._runtime

    def verify(self, checkpoint):
        limit = time.monotonic() + RPC_TIMEOUT
        try:
            _send(_TimedPipe(self.pipe, limit), {'action': 'verify'})
            while not self.pipe.poll(min(.05, _remaining(limit))):
                checkpoint()
            result = _receive(self.pipe, max_bytes=4096)
        except PIPE_ERRORS as exc:
            self.close()
            raise Failure('CONFLICT', 'Runtime lease process stopped before verification') from exc
        if result.get('state') != 'verified':
            raise Failure('CONFLICT', 'Runtime lease changed before verification: ' + result.get('reason', 'unknown'))

    def close(self):
        self.pipe.close()


def acquire(jobs_root, blender):
    """Reuse an already protected snapshot, or miss and let the caller hash."""
    deadline = time.monotonic() + RPC_TIMEOUT
    directory = _current(_root(jobs_root))
    if directory is None:
        return None
    pipe = None
    try:
        state = read_json(directory / 'ready.json')
        if (state['id'] != directory.name or not _same_path(state['blender'], blender) or
                not _same_path(state['python'], sys.executable) or
                not _same_path(state['core'], Path(__file__).parent) or
                not _alive(state['pid'])):
            return None
        authkey = _user_token(bytes.fromhex(state['auth']), decrypt=True)
        pipe = _connect(_address(state['id']), deadline)
        timed = _TimedPipe(pipe, deadline)
        answer_challenge(timed, authkey)
        deliver_challenge(timed, authkey)
        _send(timed, {'action': 'acquire'})
        result = _receive(timed, max_bytes=4096)
        if result.get('state') != 'leased' or result.get('id') != state['id']:
            return None
        lease = Lease(pipe, directory, result)
        pipe = None
        return lease
    except (Failure, KeyError, TypeError, *PIPE_ERRORS):
        return None
    finally:
        if pipe is not None:
            pipe.close()


def _serve(directory):
    spec = read_json(directory / 'spec.json')
    stop = directory / 'stop.request'
    state = {key: spec[key] for key in ('id', 'blender', 'idle_seconds', 'created_at')}
    state.update(state='starting', pid=os.getpid())
    atomic_json(directory / 'starting.json', state)
    started = time.monotonic()

    def checkpoint():
        if stop.is_file():
            raise Failure('CANCELLED', 'Runtime lease stopped during startup')
        if time.monotonic() - started > spec['startup_timeout']:
            raise Failure('TIMEOUT', 'Runtime lease startup exceeded requested timeout')

    try:
        with ExitStack() as guards:
            core = Path(__file__).resolve().parent
            watchers = [DirectoryWatch(Path(spec['blender']).parent, True, _runtime_member_changed)]
            guards.callback(watchers[0].close)
            watchers.append(DirectoryWatch(core, False, _core_member_changed))
            guards.callback(watchers[1].close)
            from pipeline import key as identity_key, runtime_identity

            runtime = runtime_identity(spec['blender'], guards, checkpoint, strict_rehash=True)
            snapshot = directory / 'snapshot.json'
            atomic_json(snapshot, runtime)
            snapshot_guard = guards.enter_context(FileGuard(snapshot))
            if not all(watcher.unchanged() for watcher in watchers):
                raise Failure('CONFLICT', 'Watched runtime changed during startup')
            authkey = _user_token(bytes.fromhex(spec['auth']), decrypt=True)
            listener = Listener(_address(spec['id']), family='AF_PIPE')
            guards.callback(listener.close)
            payload = json.dumps({'state': 'leased', 'id': spec['id'], 'runtime_key': identity_key(runtime),
                                  'registry_sha256': runtime.get('hashes', {}).get(str(core / 'registry.json')),
                                  'snapshot_sha256': snapshot_guard.sha256()},
                                 separators=(',', ':'), ensure_ascii=False).encode('utf-8')
            lock = threading.Lock()
            last = time.monotonic()
            active = 0
            closing = None

            def session(pipe):
                nonlocal active, last
                leased = False
                try:
                    timed = _TimedPipe(pipe, time.monotonic() + RPC_TIMEOUT)
                    deliver_challenge(timed, authkey)
                    answer_challenge(timed, authkey)
                    if _receive(timed, max_bytes=4096) != {'action': 'acquire'}:
                        return
                    with lock:
                        if closing or stop.is_file() or not all(watcher.unchanged() for watcher in watchers):
                            _send(timed, {'state': 'miss'})
                            return
                        active += 1
                        leased = True
                    timed.send_bytes(payload)
                    command = _receive(pipe, max_bytes=4096)
                    if command == {'action': 'verify'}:
                        valid = all(watcher.unchanged() for watcher in watchers)
                        _send(_TimedPipe(pipe, time.monotonic() + RPC_TIMEOUT), {'state': 'verified'} if valid else
                              {'state': 'invalid', 'reason': 'watched_directory_changed'})
                except PIPE_ERRORS:
                    pass
                finally:
                    if leased:
                        with lock:
                            active -= 1
                            last = time.monotonic()
                    pipe.close()

            def accept():
                while True:
                    try:
                        pipe = listener.accept()
                    except OSError:
                        return
                    threading.Thread(target=session, args=(pipe,), daemon=True).start()

            threading.Thread(target=accept, daemon=True).start()
            state.update(state='ready', ready_at=time.time(), cold_seconds=time.monotonic() - started,
                         runtime_files=len(runtime['runtime_inventory']))
            # Each receipt is created once. Concurrent readers never hold the
            # destination of an in-progress state replacement open on Windows.
            private = {key: spec[key] for key in ('core', 'python', 'auth')}
            atomic_json(directory / 'ready.json', {**state, **private})
            while True:
                with lock:
                    if not all(watcher.unchanged() for watcher in watchers):
                        closing = 'watched_directory_changed'
                    elif closing is None and stop.is_file():
                        closing = 'stop_requested'
                    elif closing is None and not active and time.monotonic() - last >= spec['idle_seconds']:
                        closing = 'idle_timeout'
                    remaining = active
                if closing is not None:
                    if not remaining:
                        break
                    if state['state'] != 'draining':
                        state.update(state='draining', reason=closing)
                        atomic_json(directory / 'draining.json', state)
                time.sleep(.05)
            for watch in watchers:
                if watch.changed is not None:
                    state['change'] = watch.changed
                    break
        state.update(state='invalid' if closing == 'watched_directory_changed' else 'stopped', reason=closing)
    except Failure as exc:
        state.update(state='stopped' if exc.code == 'CANCELLED' else 'failed',
                     reason='stop_requested' if exc.code == 'CANCELLED' else 'startup_failed',
                     error={'code': exc.code, 'message': str(exc)})
    except Exception as exc:
        state.update(state='failed', reason='worker_error', error={'code': 'WORKER_FAILED', 'message': str(exc)})
    state['closed_at'] = time.time()
    atomic_json(directory / 'closed.json', state)


if __name__ == '__main__':
    _serve(Path(sys.argv[1]).resolve())
