# SPDX-License-Identifier: GPL-3.0-or-later
"""Run: python -m unittest discover -s tests -p test_host.py -v.

Exercises the exported tree through real CLI subprocesses, without Blender.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/blenderctl/cli.py'
RIG_CLI = ROOT / 'tools/rigging_workflow_cli.py'


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, str(CLI), '--jobs-dir', str(self.work / 'jobs'), *args],
                              cwd=self.work, capture_output=True, text=True, encoding='utf-8', timeout=30)

    def rig_call(self, *args):
        return subprocess.run([sys.executable, str(RIG_CLI), *args],
                              cwd=self.work, capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_version_help_from_unrelated_directory(self):
        version = self.call('--version')
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertRegex(version.stdout.strip(), r'^\d+\.\d+\.\d+$')
        help_result = self.call('--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('material', help_result.stdout)
        self.assertFalse((self.work / 'jobs').exists())

    def test_material_describe_references_are_shipped_and_exact(self):
        result = self.call('material', 'describe')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['ok'], data)
        descriptors = []
        def walk(value):
            if isinstance(value, dict):
                if {'file', 'sha256', 'bytes'} <= value.keys():
                    descriptors.append(value)
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(data)
        self.assertGreaterEqual(len(descriptors), 10)
        for row in descriptors:
            path = Path(row['file']).resolve()
            self.assertTrue(path.is_relative_to(ROOT), row)
            content = path.read_bytes()
            self.assertEqual(len(content), row['bytes'])
            self.assertEqual(hashlib.sha256(content).hexdigest(), row['sha256'])
        self.assertFalse((self.work / 'jobs').exists())

    def test_rigging_workflow_discovers_and_dispatches_standalone_commands(self):
        checkout = self.work / 'rigging [source]'
        runtime = checkout / 'cli-v003'
        package = runtime / 'rigging_workflow'
        package.mkdir(parents=True)
        (package / '__init__.py').touch()
        scripts = runtime / 'cli'
        scripts.mkdir()
        routes = {
            'candidate-entry': ('character_entry_candidate.py', 'inspect'),
            'candidate-selected-skin': ('selected_skinning_cli.py', 'inspect'),
            'candidate-repair': ('rigging_candidate.py', 'inspect'),
            'candidate-skinning-policy': ('skinning_policy_candidate.py', 'plan'),
        }
        for filename, _ in routes.values():
            (scripts / filename).write_text(
                'import json, sys\nfrom pathlib import Path\n'
                'print(json.dumps([Path(__file__).name, *sys.argv[1:]]))\n'
                'sys.exit(17)\n', encoding='utf-8',
            )
        group = ('rigging-workflow', '--rigging-root', str(checkout))
        result = self.rig_call(*group, 'discover')
        self.assertEqual(result.returncode, 0, result.stderr)
        discovery = json.loads(result.stdout)
        self.assertEqual(discovery['edition'], '0.42.3-agent-v003')
        self.assertEqual(len(discovery['commands']), len(routes))
        self.assertTrue(all(row['available'] for row in discovery['commands']))
        request = self.work / 'source request.json'
        run = self.work / 'new run'
        blender = self.work / 'Portable Blender' / 'blender.exe'
        for route, (filename, mode) in routes.items():
            with self.subTest(route=route):
                result = self.rig_call('--blender', str(blender), *group, route, mode, '--request', str(request), '--run-dir', str(run))
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    filename, mode, '--run-dir', str(run), '--request', str(request), '--blender', str(blender),
                ])
        cancelled = self.rig_call(*group, 'candidate-repair', 'cancel', '--run-dir', str(run))
        self.assertEqual(cancelled.returncode, 17, cancelled.stderr)
        self.assertEqual(json.loads(cancelled.stdout), ['rigging_candidate.py', 'cancel', '--run-dir', str(run)])
        self.assertFalse((self.work / 'jobs').exists())

    def test_rigging_workflow_reports_missing_checkout(self):
        result = self.rig_call(
            'rigging-workflow', '--rigging-root', str(self.work / 'missing'),
            'candidate-entry', 'inspect', '--request', str(self.work / 'request.json'), '--run-dir', str(self.work / 'run'),
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'NOT_FOUND')

    def test_schema_local_references_resolve(self):
        paths = list((ROOT / 'docs/cli/schemas').glob('*.json'))
        self.assertGreater(len(paths), 50)
        def walk(value, path):
            if isinstance(value, dict):
                ref = value.get('$ref')
                if ref and not urlsplit(ref).scheme:
                    name, _, fragment = ref.partition('#')
                    target = (path.parent / unquote(name)).resolve() if name else path
                    self.assertTrue(target.is_relative_to(ROOT), ref)
                    document = json.loads(target.read_text(encoding='utf-8-sig'))
                    if fragment.startswith('/'):
                        for part in unquote(fragment)[1:].split('/'):
                            key = part.replace('~1', '/').replace('~0', '~')
                            document = document[int(key)] if isinstance(document, list) else document[key]
                for child in value.values(): walk(child, path)
            elif isinstance(value, list):
                for child in value: walk(child, path)
        for path in paths:
            doc = json.loads(path.read_text(encoding='utf-8-sig'))
            if path.name == 'material-workflow-template.schema.json':
                # This shared embedded object schema intentionally has no dialect header.
                self.assertEqual(doc['type'], 'object')
                self.assertIn('properties', doc)
                self.assertIn('required', doc)
            else:
                self.assertEqual(doc['$schema'], 'https://json-schema.org/draft/2020-12/schema')
            walk(doc, path)

    def test_invalid_request_exit_codes(self):
        request = self.work / 'bad.json'
        request.write_text(json.dumps({'schema_version': '1.0', 'command': 'invented', 'params': {}}))
        for args, code, error in [(('request', str(request)), 2, 'INVALID_REQUEST'),
                                  (('request', str(self.work / 'missing.json')), 3, 'NOT_FOUND')]:
            result = self.call(*args)
            self.assertEqual(result.returncode, code, result.stderr)
            envelope = json.loads(result.stdout)
            self.assertFalse(envelope['ok'])
            self.assertEqual(envelope['error']['code'], error)

    def test_job_id_path_traversal_rejected(self):
        for ident in ('../outside', '..\\outside', 'A' * 32, 'a' * 31):
            result = self.call('job', 'status', ident)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'INVALID_REQUEST')
        self.assertFalse((self.work / 'outside').exists())


if __name__ == '__main__':
    unittest.main()
