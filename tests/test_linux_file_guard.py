# SPDX-License-Identifier: GPL-3.0-or-later
"""Real kernel lease/namespace tests. Run on local tmpfs/ext*/XFS/Btrfs.

No sysctl changes or privileged operations. Fixtures are generated under /tmp;
unsupported hosts skip only real-kernel cases, with the exact reason reported.
"""
import errno
import gc
import hashlib
import mmap
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools/blenderctl'
sys.path.insert(0, str(TOOLS))
from protocol import Failure
from filesystem import FileGuard, PathLocks, check_all_guards, native_path

if sys.platform == 'linux':
    import fcntl
    import linux_file_guard as impl


@unittest.skipUnless(sys.platform == 'linux', 'Linux kernel semantics')
class LinuxFileGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = tempfile.TemporaryDirectory(prefix='blenderctl-guard-probe-', dir='/tmp')
        try:
            probe = Path(cls.probe.name) / 'probe'
            probe.write_bytes(b'probe')
            try:
                with FileGuard(probe):
                    pass
            except Failure as exc:
                if exc.code == 'UNSUPPORTED':
                    raise unittest.SkipTest(str(exc)) from exc
                raise
        finally:
            cls.probe.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='blenderctl-guard-test-', dir='/tmp')
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'source.blend'
        self.content = b'generated guard fixture\0' * 1024
        self.path.write_bytes(self.content)
        self.guards = []

    def tearDown(self):
        for guard in reversed(self.guards):
            guard.close(validate=False)
        self.temp.cleanup()
        self.assertIsNone(impl._sink, 'lease-signal thread was leaked')
        self.assertFalse(list(impl._registry), 'active guard registry was leaked')

    def guard(self, path=None):
        guard = FileGuard(path or self.path)
        self.guards.append(guard)
        return guard

    def run_python(self, code, *args):
        return subprocess.run([sys.executable, '-c', code, *map(str, args)],
                              capture_output=True, text=True, timeout=10)

    def expect_conflict(self, operation):
        with self.assertRaises(Failure) as caught:
            operation()
        self.assertEqual(caught.exception.code, 'CONFLICT')

    def test_hash_identity_chunks_and_no_global_signal_changes(self):
        handler = signal.getsignal(signal.SIGIO)
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        guard = self.guard()
        self.assertEqual(guard.sha256(), hashlib.sha256(self.content).hexdigest())
        self.assertEqual(b''.join(guard.chunks()), self.content)
        self.assertEqual(guard.identity(), {'volume': self.path.stat().st_dev,
                                             'file_index': self.path.stat().st_ino})
        check_all_guards()
        self.assertEqual(signal.getsignal(signal.SIGIO), handler)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), mask)
        self.assertEqual(native_path(self.path), str(self.path))
        self.assertFalse(os.get_inheritable(guard.handle))
        self.assertFalse(os.get_inheritable(guard._notify_fd))

    def test_existing_writer_descriptor_rejected_without_leak(self):
        before = set(os.listdir('/proc/self/fd'))
        with self.path.open('r+b'):
            for _ in range(10):
                self.expect_conflict(lambda: FileGuard(self.path))
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        self.assertIsNone(impl._sink)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_existing_writable_mapping_rejected_after_fd_close(self):
        fd = os.open(self.path, os.O_RDWR)
        mapping = mmap.mmap(fd, 0, access=mmap.ACCESS_WRITE)
        os.close(fd)
        try:
            self.expect_conflict(lambda: FileGuard(self.path))
        finally:
            mapping.close()
        self.guard().check()

    def test_other_process_preopened_writer_rejected(self):
        proc = subprocess.Popen([sys.executable, '-c',
            'import os,sys; f=os.open(sys.argv[1],os.O_WRONLY); '
            'print("ready",flush=True); sys.stdin.read(); os.close(f)', str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            self.expect_conflict(lambda: FileGuard(self.path))
        finally:
            proc.communicate('', timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_concurrent_nonblocking_write_denied_and_break_is_sticky(self):
        guard = self.guard()
        result = self.run_python(
            'import os,sys,errno\ntry: os.open(sys.argv[1],os.O_WRONLY|os.O_NONBLOCK)\n'
            'except OSError as e: sys.exit(0 if e.errno==errno.EWOULDBLOCK else 2)\n'
            'sys.exit(3)', self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), self.content)
        self.expect_conflict(guard.check)
        self.expect_conflict(guard.sha256)
        self.expect_conflict(check_all_guards)
        self.expect_conflict(guard.close)
        self.assertIsNone(guard.handle)
        # A failed validation still releases the kernel lease.
        fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)

    def test_blocking_writer_waits_until_guard_releases(self):
        guard = self.guard()
        proc = subprocess.Popen([sys.executable, '-c',
            'import os,sys; print("ready",flush=True); '
            'f=os.open(sys.argv[1],os.O_WRONLY); os.write(f,b"changed"); os.close(f)', str(self.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            deadline = time.monotonic() + 5
            while fcntl.fcntl(guard.handle, fcntl.F_GETLEASE) == fcntl.F_RDLCK:
                if time.monotonic() > deadline:
                    self.fail('writer did not request a lease break')
                time.sleep(.005)
            self.assertIsNone(proc.poll())
            self.assertEqual(self.path.read_bytes(), self.content)
            self.expect_conflict(guard.close)
            proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(self.path.read_bytes().startswith(b'changed'))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)

    def test_same_process_writer_does_not_terminate_on_sigio(self):
        handler = signal.getsignal(signal.SIGIO)
        guard = self.guard()
        with self.assertRaises(BlockingIOError):
            os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        self.expect_conflict(guard.check)
        self.assertEqual(signal.getsignal(signal.SIGIO), handler)

    def test_existing_sigio_handler_is_preserved_and_not_called(self):
        observed = []
        previous = signal.signal(signal.SIGIO, lambda *args: observed.append(args))
        try:
            guard = self.guard()
            with self.assertRaises(BlockingIOError):
                os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            self.expect_conflict(guard.check)
            guard.close(validate=False)
            self.assertEqual(observed, [])
        finally:
            signal.signal(signal.SIGIO, previous)

    def test_nested_guards_keep_independent_leases(self):
        outer = self.guard()
        inner = self.guard()
        self.assertIs(outer._signal_sink, inner._signal_sink)
        self.assertEqual(outer._signal_sink.references, 2)
        inner.close()
        outer.check()
        self.assertEqual(outer._signal_sink.references, 1)
        self.assertEqual(outer.sha256(), hashlib.sha256(self.content).hexdigest())
        with self.assertRaises(BlockingIOError):
            os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        self.expect_conflict(outer.check)

    def test_subprocess_can_take_independent_read_guard(self):
        guard = self.guard()
        result = self.run_python(
            'import sys; sys.path.insert(0,sys.argv[1]); from filesystem import FileGuard\n'
            'with FileGuard(sys.argv[2]) as g: print(g.sha256())', TOOLS, self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), guard.sha256())
        guard.check()

    def test_guard_from_nonmain_thread_and_nested_main_guard(self):
        outer = self.guard()
        errors = []
        def check_thread():
            try:
                with FileGuard(self.path) as guard:
                    guard.check()
                    self.assertEqual(guard.sha256(), outer.sha256())
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=check_thread)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        outer.check()

    def test_final_and_ancestor_symlinks_forbidden(self):
        link = self.directory / 'link'
        link.symlink_to(self.path)
        self.expect_conflict(lambda: FileGuard(link))
        directory_link = self.directory / 'directory-link'
        directory_link.symlink_to(self.directory, target_is_directory=True)
        self.expect_conflict(lambda: FileGuard(directory_link / self.path.name))
        self.expect_conflict(lambda: FileGuard(str(directory_link / '..' / self.path.name)))

    def test_nonregular_files_fail_without_blocking(self):
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        self.expect_conflict(lambda: FileGuard(fifo))
        self.expect_conflict(lambda: FileGuard(self.directory))

    def test_rename_and_restore_rejected_even_though_identity_is_same(self):
        guard = self.guard()
        original = guard.identity()
        moved = self.directory / 'moved'
        self.path.rename(moved)
        moved.rename(self.path)
        self.assertEqual(original['file_index'], self.path.stat().st_ino)
        self.expect_conflict(guard.check)
        self.expect_conflict(guard.check)

    def test_replacement_then_restore_rejected(self):
        guard = self.guard()
        other = self.directory / 'other'
        old = self.directory / 'old'
        other.write_bytes(self.content)
        self.path.rename(old)
        other.rename(self.path)
        self.path.rename(other)
        old.rename(self.path)
        self.expect_conflict(guard.check)

    def test_replacement_with_symlink_rejected(self):
        guard = self.guard()
        moved = self.directory / 'old'
        self.path.rename(moved)
        self.path.symlink_to(moved)
        self.expect_conflict(guard.check)

    def test_ancestor_rename_then_restore_rejected(self):
        folder = self.directory / 'folder'
        folder.mkdir()
        path = folder / 'source'
        path.write_bytes(self.content)
        guard = self.guard(path)
        moved = self.directory / 'renamed'
        folder.rename(moved)
        moved.rename(folder)
        self.expect_conflict(guard.check)

    def test_unlink_rejected(self):
        guard = self.guard()
        self.path.unlink()
        self.expect_conflict(guard.check)

    def test_unrelated_sibling_namespace_change_does_not_invalidate(self):
        guard = self.guard()
        sibling = self.directory / 'sibling'
        sibling.write_bytes(b'other')
        sibling.rename(self.directory / 'renamed-sibling')
        (self.directory / 'renamed-sibling').unlink()
        guard.check()

    def test_metadata_changes_are_detected(self):
        guard = self.guard()
        initial = self.path.stat()
        os.utime(self.path, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1))
        os.utime(self.path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        self.expect_conflict(guard.check)

    def test_hardlink_writer_is_blocked(self):
        link = self.directory / 'hardlink'
        os.link(self.path, link)
        guard = self.guard()
        with self.assertRaises(BlockingIOError):
            os.open(link, os.O_WRONLY | os.O_NONBLOCK)
        self.expect_conflict(guard.check)

    def test_mutation_during_hash_checkpoint_invalidates_result(self):
        guard = self.guard()
        def checkpoint():
            if self.path.exists():
                self.path.rename(self.directory / 'renamed')
        self.expect_conflict(lambda: guard.sha256(checkpoint))

    def test_clean_context_exit_validates_and_closes(self):
        guard = self.guard()
        with self.assertRaises(Failure) as caught:
            with guard:
                self.path.rename(self.directory / 'moved')
        self.assertEqual(caught.exception.code, 'CONFLICT')
        self.assertIsNone(guard.handle)
        self.assertIsNone(guard._notify_fd)
        self.assertEqual(guard._directories, [])

    def test_exception_exit_keeps_original_failure_and_closes(self):
        guard = self.guard()
        with self.assertRaisesRegex(ValueError, 'original'):
            with guard:
                self.path.rename(self.directory / 'moved')
                raise ValueError('original')
        self.assertIsNone(guard.handle)
        self.assertIsNone(guard._notify_fd)

    def test_cleanup_io_failure_is_a_protocol_failure_after_release(self):
        guard = self.guard()
        original = guard._close_fds
        def failed_close(**kwargs):
            original(**kwargs)
            raise OSError(errno.EIO, 'fixture cleanup failure')
        with patch.object(guard, '_close_fds', side_effect=failed_close):
            with self.assertRaises(Failure) as caught:
                guard.close()
        self.assertEqual(caught.exception.code, 'IO_ERROR')
        self.assertIsNone(guard.handle)
        self.assertIsNone(impl._sink)

    def test_lease_revocation_even_without_notifications_fails_closed(self):
        guard = self.guard()
        # Simulates the kernel's forced-break outcome without changing sysctls
        # or waiting for the global lease-break timeout.
        fcntl.fcntl(guard.handle, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        self.expect_conflict(guard.check)

    def test_watch_overflow_is_sticky_conflict(self):
        guard = self.guard()
        overflow = impl._EVENT.pack(-1, impl._IN_Q_OVERFLOW, 0, 0)
        with patch.object(impl.os, 'read', return_value=overflow):
            self.expect_conflict(guard.check)
        self.expect_conflict(guard.check)

    def test_watch_loss_is_conflict(self):
        guard = self.guard()
        wd = next(iter(guard._watches))
        event = impl._EVENT.pack(wd, impl._IN_IGNORED, 0, 0)
        with patch.object(impl.os, 'read', return_value=event):
            self.expect_conflict(guard.check)

    def test_owner_change_fails_closed(self):
        guard = self.guard()
        # Route elsewhere but do not trigger a break until cleanup.
        fcntl.fcntl(guard.handle, fcntl.F_SETOWN, os.getpid())
        self.expect_conflict(guard.check)

    def test_unsupported_filesystem_no_fallback_or_descriptor_leak(self):
        before = set(os.listdir('/proc/self/fd'))
        with patch.object(impl, '_filesystem_type', return_value=0x794c7630):
            with self.assertRaises(Failure) as caught:
                FileGuard(self.path)
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_unsupported_lease_capability_no_fallback_or_leak(self):
        before = set(os.listdir('/proc/self/fd'))
        original = fcntl.fcntl
        def unsupported(fd, cmd, *args):
            if cmd == fcntl.F_SETLEASE and args == (fcntl.F_RDLCK,):
                raise OSError(errno.EOPNOTSUPP, 'fixture unsupported lease')
            return original(fd, cmd, *args)
        with patch.object(fcntl, 'fcntl', side_effect=unsupported):
            with self.assertRaises(Failure) as caught:
                FileGuard(self.path)
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        self.assertIsNone(impl._sink)

    def test_namespace_mutation_during_acquisition_is_rejected(self):
        original = impl.LinuxFileGuard._watch
        changed = False
        def watch(guard, fd, child=None):
            nonlocal changed
            original(guard, fd, child)
            if child == self.path.name and not changed:
                changed = True
                moved = self.directory / 'during-acquisition'
                self.path.rename(moved)
                moved.rename(self.path)
        before = set(os.listdir('/proc/self/fd'))
        with patch.object(impl.LinuxFileGuard, '_watch', new=watch):
            self.expect_conflict(lambda: FileGuard(self.path))
        self.assertTrue(changed)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        self.assertIsNone(impl._sink)

    def test_fstat_acquisition_failure_closes_new_directory_fd(self):
        before = set(os.listdir('/proc/self/fd'))
        with patch.object(impl.os, 'fstat', side_effect=OSError(errno.EIO, 'fixture')):
            with self.assertRaises(Failure):
                FileGuard(self.path)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_watch_install_failure_closes_all_descriptors(self):
        before = set(os.listdir('/proc/self/fd'))
        with patch.object(impl.LinuxFileGuard, '_watch', side_effect=OSError(errno.ENOSPC, 'fixture')):
            with self.assertRaises(Failure):
                FileGuard(self.path)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_repeated_open_close_and_gc_release_everything(self):
        before = set(os.listdir('/proc/self/fd'))
        for _ in range(30):
            with FileGuard(self.path) as guard:
                guard.check()
        guard = FileGuard(self.path)
        del guard
        gc.collect()
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        self.assertIsNone(impl._sink)
        self.assertFalse(list(impl._registry))

    def test_fork_child_does_not_revoke_parent_lease(self):
        guard = self.guard()
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    guard.check()
                except Failure as exc:
                    assert exc.code == 'UNSUPPORTED'
                else:
                    os._exit(2)
                with FileGuard(self.path) as child_guard:
                    child_guard.check()
                os._exit(0)
            except BaseException:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        guard.check()
        self.assertEqual(guard.sha256(), hashlib.sha256(self.content).hexdigest())

    def test_transaction_apis_stay_unsupported(self):
        for kwargs in ({'renameable': True}, {'directory': True}):
            with self.assertRaises(Failure) as caught:
                FileGuard(self.path, **kwargs)
            self.assertEqual(caught.exception.code, 'UNSUPPORTED')
        with self.assertRaises(Failure) as caught:
            PathLocks([self.path])
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')
        with self.assertRaises(Failure) as caught:
            self.guard().rename_new(self.directory / 'destination')
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')


if __name__ == '__main__':
    unittest.main()
