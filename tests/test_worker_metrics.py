# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/blenderctl'))
import worker_metrics
from protocol import Failure


class WorkerMetricsTests(unittest.TestCase):
    def test_linux_kib_converted_to_bytes(self):
        resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: SimpleNamespace(ru_maxrss=123))
        with patch.object(worker_metrics.sys, 'platform', 'linux'), patch.dict(sys.modules, {'resource': resource}):
            self.assertEqual(worker_metrics.peak_memory(), 123 * 1024)

    @unittest.skipUnless(sys.platform in ('linux', 'win32'), 'Supported worker platform only')
    def test_real_process_peak_is_positive(self):
        self.assertGreater(worker_metrics.peak_memory(), 0)

    def test_unknown_platform_refuses(self):
        with patch.object(worker_metrics.sys, 'platform', 'unknown'), self.assertRaises(Failure) as caught:
            worker_metrics.peak_memory()
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')
