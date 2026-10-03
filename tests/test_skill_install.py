# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual installation behavior in an owned temporary destination."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'scripts/install_skills.py'


class SkillInstallTests(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, '-B', str(INSTALL), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, encoding='utf-8')

    def test_install_preserves_source_and_all_four_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'skills'
            result = self.call('--destination', destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = json.loads(result.stdout)['installed']
            self.assertEqual(len(installed), 4)
            for name in installed:
                for source in (ROOT / 'skills' / name).rglob('*'):
                    if source.is_file() and '__pycache__' not in source.parts:
                        self.assertEqual(source.read_bytes(), (destination / name / source.relative_to(ROOT / 'skills' / name)).read_bytes())

    def test_existing_skill_refuses_before_any_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            existing = destination / 'blender-material-authoring'
            existing.mkdir()
            marker = existing / 'existing.md'
            marker.write_bytes(b'existing user content')
            result = self.call('--destination', destination)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['mutations'], 0)
            self.assertEqual(marker.read_bytes(), b'existing user content')
            self.assertEqual(list(destination.iterdir()), [existing])

    def test_list_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'not-created'
            result = self.call('--list', '--destination', destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['mutations'], 0)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
