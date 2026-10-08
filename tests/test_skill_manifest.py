# SPDX-License-Identifier: GPL-3.0-or-later
"""Manifest-based skill count and per-skill version regression checks."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('skill_checker', ROOT / 'scripts/check_skills.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class SkillManifestTests(unittest.TestCase):
    def check_variant(self, transform):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / 'skills', root / 'skills')
            manifest_path = root / 'skills/manifest.json'
            manifest = json.loads(manifest_path.read_text())
            transform(manifest, root / 'skills')
            manifest_path.write_text(json.dumps(manifest))
            with patch.object(CHECKER, 'ROOT', root), patch.object(CHECKER, 'SKILLS', root / 'skills'):
                return CHECKER.check()

    def test_mixed_versions_and_fifth_skill_pass(self):
        result = self.check_variant(lambda manifest, skills: None)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(len(result['skills']), 5)
        self.assertIn('blender-hard-surface-authoring', result['skills'])

    def test_duplicate_name_rejected(self):
        result = self.check_variant(lambda m, s: m['skills'].append(m['skills'][0].copy()))
        self.assertFalse(result['ok'])
        self.assertIn('Bundle must identify unique companion skills', result['errors'])

    def test_wrong_per_skill_version_rejected(self):
        result = self.check_variant(lambda m, s: m['skills'][0].update(version='9.9.9'))
        self.assertFalse(result['ok'])
        self.assertIn('blender-cli: skill metadata version mismatch', result['errors'])

    def test_independent_bundle_version_does_not_rewrite_skills(self):
        result = self.check_variant(lambda m, s: m.update(bundle_version='0.9.0-dev.1'))
        self.assertTrue(result['ok'], result['errors'])


if __name__ == '__main__':
    unittest.main()
