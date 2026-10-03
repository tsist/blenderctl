# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for explicit platform refusal and terminal job records."""
import json
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
import pipeline
from protocol import Failure


class PlatformBoundaryTests(unittest.TestCase):
    def test_pipeline_lock_refusal_has_terminal_result(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / ('a' * 32)
            job.mkdir()
            (job / 'request.json').write_text(json.dumps({
                'schema_version': '1.0', 'command': 'pipeline.plan', 'params': {}}))
            (job / 'status.json').write_text(json.dumps({'state': 'queued'}))
            with patch.object(pipeline, 'PathLocks', side_effect=Failure('UNSUPPORTED', 'test boundary')):
                result, code = pipeline.execute(job)
            self.assertEqual(code, 10)
            self.assertFalse(result['ok'])
            self.assertEqual(result['command'], 'pipeline.plan')
            self.assertEqual(result, json.loads((job / 'result.json').read_text()))
            self.assertEqual(json.loads((job / 'status.json').read_text())['state'], 'failed')


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('BLENDERCTL_TEST_BLENDER'),
                     'Set BLENDERCTL_TEST_BLENDER for native Linux race test')
class NativeGuardAcceptanceTests(unittest.TestCase):
    def test_write_open_attempt_rejects_job_and_preserves_source(self):
        blender = os.environ['BLENDERCTL_TEST_BLENDER']
        with tempfile.TemporaryDirectory(prefix='blenderctl-native-race-', dir='/tmp') as temp:
            root = Path(temp)
            fixture = subprocess.run([blender, '--background', '--factory-startup', '--disable-autoexec',
                                      '--offline-mode', '--python-exit-code', '1', '--python',
                                      str(ROOT / 'tests/blender_smoke.py'), '--', 'fixture', str(root), str(ROOT)],
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(fixture.returncode, 0, fixture.stderr)
            request = json.loads((root / 'request.json').read_text())
            request['params']['manifest']['preview'].update(width=512, height=512, samples=128)
            (root / 'request.json').write_text(json.dumps(request))
            source = root / 'source.blend'
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            base = [sys.executable, str(ROOT / 'tools/blenderctl/cli.py'), '--blender', blender,
                    '--jobs-dir', str(root / 'jobs')]
            launch = subprocess.run([*base, '--async', 'request', str(root / 'request.json')],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(launch.returncode, 0, launch.stdout + launch.stderr)
            job_id = json.loads(launch.stdout)['job_id']
            job = root / 'jobs' / job_id
            deadline = time.monotonic() + 10
            while not (job / 'worker.gate').exists() and time.monotonic() < deadline:
                self.assertFalse((job / 'result.json').exists(), 'Job ended before its guarded worker started')
                time.sleep(.01)
            self.assertTrue((job / 'worker.gate').exists())
            # No bytes are written. Even this rejected write-open attempt
            # must permanently invalidate the lease and job acceptance.
            with self.assertRaises(OSError) as caught:
                fd = os.open(source, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            self.assertEqual(caught.exception.errno, errno.EAGAIN)
            completed = subprocess.run([*base, 'job', 'wait', job_id, '--wait-seconds', '30'],
                                       capture_output=True, text=True, timeout=40)
            self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertFalse(result['ok'])
            self.assertIsNone(result['data'])
            self.assertEqual(result['error']['code'], 'CONFLICT')
            self.assertEqual(json.loads((job / 'status.json').read_text())['state'], 'failed')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)


if __name__ == '__main__':
    unittest.main()
