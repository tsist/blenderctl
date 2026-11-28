"""Windows actual-worker ownership gate; standard library only, before business imports.

Native signatures reuse resource_leases.py and win_appcontainer.py; image query
and FILETIME layout were checked against local Windows SDK 10.0.26100.0.
The profile venv launcher PID is not accepted as the executing worker identity.
"""
import ctypes
from ctypes import wintypes as w
import json
import math
import os
from pathlib import Path
import re
import time
import uuid


class OwnedWorkerError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _need(condition, message, code='CONFLICT'):
    if not condition:
        raise OwnedWorkerError(code, message)


def _api():
    _need(os.name == 'nt', 'Actual worker ownership requires Windows', 'UNSUPPORTED')
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = (
        ('GetCurrentProcess', [], w.HANDLE),
        ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ('GetProcessTimes', [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                            ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)], w.BOOL),
        ('QueryFullProcessImageNameW', [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        ('IsProcessInJob', [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
        ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
        ('CloseHandle', [w.HANDLE], w.BOOL),
    )
    for name, args, result in signatures:
        function = getattr(k, name)
        function.argtypes = args
        function.restype = result
    return k


def _checked(result, operation='Win32'):
    if not result:
        error = ctypes.WinError(ctypes.get_last_error())
        error.operation = operation
        raise error
    return result


def _timeout(value):
    _need(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10,
          'Worker gate timeout must be finite, positive and at most ten seconds', 'INVALID_REQUEST')
    return float(value)


def _signature(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None,
          'Typed SHA-256 request signature required', 'INVALID_REQUEST')
    return value


def _directory(out):
    path = Path(out)
    _need(path.is_absolute() and path.is_dir() and not str(path).startswith('\\\\'),
          'Existing absolute local owned output directory required', 'INVALID_REQUEST')
    return path


def _publish(path, value):
    temp = path.with_name('.' + uuid.uuid4().hex + '.owned-worker.partial')
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows rename is atomic and refuses to replace existing authority.
    os.rename(temp, path)


def _pairs(items):
    result = {}
    for key, value in items:
        _need(key not in result, 'Duplicate worker authority key')
        result[key] = value
    return result


def _read_wait(path, deadline, checkpoint):
    sharing_attempts = 0
    while True:
        checkpoint()
        _need(time.monotonic() < deadline, 'Actual worker gate deadline exceeded', 'TIMEOUT')
        try:
            with path.open('rb') as stream:
                data = stream.read(16385)
            _need(len(data) <= 16384, 'Worker authority exceeds finite document budget', 'RESOURCE_LIMIT')
            value = json.loads(data, object_pairs_hook=_pairs)
            _need(type(value) is dict, 'Worker authority must be an object')
            return value
        except FileNotFoundError:
            time.sleep(.01)
        except PermissionError as exc:
            # CRT read_bytes can omit winerror for transient sharing denials.
            # Four attempts at most; persistent denial remains fatal.
            sharing_attempts += 1
            if sharing_attempts >= 4:
                raise
            time.sleep(.005)


def _process_identity(k, handle, pid):
    values = [w.FILETIME() for _ in range(4)]
    _checked(k.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]), 'GetProcessTimes')
    created = (int(values[0].dwHighDateTime) << 32) | int(values[0].dwLowDateTime)
    size = w.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    _checked(k.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)), 'QueryFullProcessImageNameW')
    _need(created > 0 and size.value > 0, 'Empty native process identity')
    return {'pid': pid, 'creation_FILETIME': str(created), 'actual_exe': image.value}


def _validate_identity(value, signature):
    _need(set(value) == {'schema', 'request_signature', 'pid', 'creation_FILETIME', 'actual_exe'},
          'Worker identity fields differ')
    _need(value['schema'] == 'relight-session-worker-identity-v1' and value['request_signature'] == signature,
          'Worker identity request binding differs')
    _need(type(value['pid']) is int and 0 < value['pid'] <= 0xffffffff, 'Typed positive worker PID required')
    created = value['creation_FILETIME']
    _need(type(created) is str and re.fullmatch('[1-9][0-9]{0,19}', created) is not None
          and int(created) <= 0xffffffffffffffff, 'Decimal native creation FILETIME required')
    _need(type(value['actual_exe']) is str and bool(value['actual_exe'])
          and Path(value['actual_exe']).is_absolute(), 'Absolute native worker image required')


def worker_gate(out, request_signature, timeout_seconds=10):
    """Publish this actual process identity; wait for matching Host release before work."""
    path = _directory(out)
    signature = _signature(request_signature)
    deadline = time.monotonic() + _timeout(timeout_seconds)
    k = _api()
    identity = {'schema': 'relight-session-worker-identity-v1', 'request_signature': signature,
                **_process_identity(k, k.GetCurrentProcess(), os.getpid())}
    _publish(path / 'worker-identity.json', identity)
    release = _read_wait(path / 'worker-release.json', deadline, lambda: None)
    expected = {**identity, 'schema': 'relight-session-worker-release-v1', 'job_membership_verified': True}
    _need(release == expected and type(release.get('pid')) is int
          and release.get('job_membership_verified') is True, 'Worker release identity differs')
    _need(_process_identity(k, k.GetCurrentProcess(), os.getpid()) ==
          {key: identity[key] for key in ('pid', 'creation_FILETIME', 'actual_exe')},
          'Worker native identity changed during gate')
    return identity


class ClaimedWorker:
    """One retained live native handle; never reopen or terminate a process by PID."""
    def __init__(self, kernel, handle, facts):
        self.kernel = kernel
        self.handle = handle
        self.facts = facts

    def wait_exit(self, timeout_seconds=10, checkpoint=None):
        _need(self.handle is not None, 'Claimed worker handle is closed')
        deadline = time.monotonic() + _timeout(timeout_seconds)
        checkpoint = checkpoint or (lambda: None)
        while True:
            checkpoint()
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result == 0:
                self.facts['exit_confirmed'] = True
                return True
            if result == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            _need(result == 258, 'Unexpected process wait result')
            _need(time.monotonic() < deadline, 'Claimed actual worker did not exit', 'TIMEOUT')
            time.sleep(.01)

    def close(self):
        if self.handle is not None:
            _checked(self.kernel.CloseHandle(self.handle), 'CloseHandle')
            self.handle = None
            self.facts['handle_closed'] = True


def claim_worker(tree, out, signature, checkpoint, timeout=10, expected_image=None):
    """Validate actual identity, attach to this live Job, retain handle, then release."""
    path = _directory(out)
    signature = _signature(signature)
    deadline = time.monotonic() + _timeout(timeout)
    _need(callable(checkpoint), 'Checkpoint callback required', 'INVALID_REQUEST')
    k = _api()
    _need(getattr(tree, 'handle', None), 'Live owned Job handle required')
    identity = _read_wait(path / 'worker-identity.json', deadline, checkpoint)
    _validate_identity(identity, signature)
    _need(identity['pid'] == tree.process.pid,
          'Worker must be the directly created root; launcher descendants are unsupported')
    if expected_image is not None:
        _need(os.path.normcase(identity['actual_exe']) == os.path.normcase(str(expected_image)),
              'Actual worker image differs from guarded native interpreter')
    # SDK PROCESS_TERMINATE=1, PROCESS_SET_QUOTA=0x100 for assignment;
    # PROCESS_QUERY_LIMITED_INFORMATION=0x1000 and SYNCHRONIZE=0x100000.
    handle = k.OpenProcess(1 | 0x100 | 0x1000 | 0x100000, False, identity['pid'])
    _checked(handle, 'OpenProcess(actual-worker claim rights)')
    claimed = None
    try:
        checkpoint()
        _need(k.WaitForSingleObject(handle, 0) == 258, 'Reported worker is no longer live')
        actual = _process_identity(k, handle, identity['pid'])
        _need(actual == {key: identity[key] for key in ('pid', 'creation_FILETIME', 'actual_exe')},
              'Reported worker native identity differs; refusing assignment/release')
        member = w.BOOL()
        _checked(k.IsProcessInJob(handle, tree.handle, ctypes.byref(member)), 'IsProcessInJob(before claim)')
        before = bool(member.value)
        _need(before, 'Direct worker is outside the owned Job; refusing business release')
        checkpoint()
        _need(time.monotonic() < deadline, 'Worker claim deadline exceeded', 'TIMEOUT')
        _need(k.WaitForSingleObject(handle, 0) == 258, 'Worker exited before release')
        facts = {**identity, 'member_before': before, 'assigned': False,
                 'job_membership_verified': True, 'exit_confirmed': False, 'handle_closed': False}
        claimed = ClaimedWorker(k, handle, facts)
        _publish(path / 'worker-release.json',
                 {**identity, 'schema': 'relight-session-worker-release-v1', 'job_membership_verified': True})
        return claimed
    except BaseException:
        # If assignment succeeded, the caller's existing Job reaper remains authority.
        # If it failed, the worker gate self-times out without starting business work.
        _checked(k.CloseHandle(handle), 'CloseHandle(failed claim)')
        if claimed is not None:
            claimed.handle = None
        raise
