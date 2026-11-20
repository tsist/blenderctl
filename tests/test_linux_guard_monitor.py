# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Linux queue smoke. Run on a qualified local filesystem, e.g. /tmp tmpfs."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

if sys.platform == 'linux':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/blenderctl'))
    from linux_file_guard import LinuxFileGuard, LinuxGuardMonitor
    from protocol import Failure


@unittest.skipUnless(sys.platform == 'linux', 'Linux kernel leases and inotify')
class LinuxGuardMonitorTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        root = tempfile.TemporaryDirectory(dir=os.environ.get('GUARD_TEST_ROOT', '/tmp'))
        self.resources.enter_context(root)
        self.root = Path(root.name)

    def files(self, count=1):
        for n in range(count):
            path = self.root / f'z{n}'
            path.write_bytes(f'payload {n}'.encode())
            yield path

    def guard(self, monitor, path):
        return self.resources.enter_context(LinuxFileGuard(path, monitor=monitor))

    def assert_rejected(self, guard):
        with self.assertRaises(Failure) as caught:
            guard.check()
        self.assertEqual(caught.exception.code, 'CONFLICT')
        guard.close(validate=False)

    def test_shared_queue_can_guard_more_files_than_default_instance_limit(self):
        paths = list(self.files(200))
        monitor = self.resources.enter_context(LinuxGuardMonitor())
        guards = [self.guard(monitor, path) for path in paths]
        self.assertTrue(all(guard._notify_fd is None for guard in guards))
        for n, guard in enumerate(guards):
            self.assertEqual(guard.sha256(), hashlib.sha256(f'payload {n}'.encode()).hexdigest())

    def test_dispatch_preserves_event_when_other_guard_drains_queue(self):
        target, unrelated = self.files(2)
        monitor = self.resources.enter_context(LinuxGuardMonitor())
        target_guard, unrelated_guard = (self.guard(monitor, p) for p in (target, unrelated))
        staging = target.with_name('renamed')
        target.rename(staging)
        staging.rename(target)
        unrelated_guard.check()
        self.assert_rejected(target_guard)

    def test_lease_break_remains_a_conflict(self):
        path, = self.files()
        monitor = self.resources.enter_context(LinuxGuardMonitor())
        guard = self.guard(monitor, path)
        command = [sys.executable, '-c', 'import os,sys; os.close(os.open(sys.argv[1],os.O_WRONLY))', str(path)]
        with subprocess.Popen(command) as process:
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        guard.check()
                    except Failure as error:
                        self.assertEqual(error.code, 'CONFLICT')
                        self.assert_rejected(guard)
                        self.assertEqual(process.wait(timeout=5), 0)
                        return
                    time.sleep(.02)
                self.fail('writer did not break the guarded lease')
            finally:
                if process.poll() is None:
                    process.kill()

    def test_monitor_loss_fails_closed(self):
        path, = self.files()
        monitor = self.resources.enter_context(LinuxGuardMonitor())
        guard = self.guard(monitor, path)
        monitor.__exit__(None, None, None)
        self.assert_rejected(guard)


if __name__ == '__main__':
    unittest.main()
