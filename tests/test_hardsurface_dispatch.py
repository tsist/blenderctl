# SPDX-License-Identifier: GPL-3.0-or-later
"""Source-only integration checks: no Blender, network, installation or user assets."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/blenderctl/cli.py'
MODULE = ROOT / 'tools/blenderctl/hardsurface_dispatch.py'
SPEC = importlib.util.spec_from_file_location('hardsurface_dispatch_test_subject', MODULE)
dispatcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatcher)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux integration only')
class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='blenderctl-dispatch-tests-')
        self.work = Path(self.temp.name)
        self.plugin = self.work / 'reviewed plugin'
        self.plugin.mkdir()
        for name in dispatcher._REQUIRED_FILES:
            path = self.plugin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('' if path.suffix == '.py' else '{}')
        (self.plugin / 'blender_manifest.toml').write_text('schema_version="1.0.0"\nid="hard_surface_workbench"\nversion="0.2.0"\ntype="add-on"\n')
        (self.plugin / 'hardsurface/__init__.py').write_text('__version__="0.2.0"\n')
        (self.plugin / 'hardsurface/host.py').write_text('import json, os, sys\nprint(json.dumps({"argv":sys.argv[1:], "cwd":os.getcwd(), "path":sys.path, "dont_write":sys.dont_write_bytecode, "no_site":sys.flags.no_site, "safe_path":sys.flags.safe_path, "blender":os.environ.get("BLENDER_PATH"), "cache":sys.pycache_prefix}))\nraise SystemExit(int(os.environ.get("HS_TEST_EXIT", "0")))\n')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'BLENDERCTL_', 'HS_TEST_'))}
        self.env.update(BLENDERCTL_HARDSURFACE_ROOT=str(self.plugin), PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        self.temp.cleanup()  # Only this test's generated metadata/source fixtures.

    def call(self, *args, env=None):
        return subprocess.run([sys.executable, '-B', str(CLI), *args], cwd=self.work, env=self.env if env is None else env, capture_output=True, text=True, timeout=20)

    def response(self, *args, env=None):
        result = self.call(*args, env=env)
        self.assertEqual(result.stderr, '', result.stderr)
        return result, json.loads(result.stdout)

    def assertFailure(self, detail, *args, env=None):
        result, data = self.response(*args, env=env)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error']['detail_code'], detail, result.stdout)

    def marker(self, name='unrelated-job-location', **overrides):
        root = self.work / name
        root.mkdir()
        owner = dict(kind='hardsurface_job_store', version=1, root=str(root), uid=os.geteuid())
        owner.update(overrides)
        (root / 'owner.json').write_text(json.dumps(owner))
        return root

    def test_missing_and_relative_root_fail_structurally(self):
        env = dict(self.env); env.pop('BLENDERCTL_HARDSURFACE_ROOT')
        self.assertFailure('HARDSURFACE_ROOT_REQUIRED', 'hardsurface', 'describe', env=env)
        env['BLENDERCTL_HARDSURFACE_ROOT'] = 'reviewed plugin'
        self.assertFailure('HARDSURFACE_UNSAFE_PATH', 'hardsurface', 'describe', env=env)

    def test_invalid_host_syntax_is_structured(self):
        (self.plugin / 'hardsurface/host.py').write_text('if broken syntax:')
        self.assertFailure('HARDSURFACE_DISPATCH_FAILED', 'hardsurface', 'describe')

    def test_abbreviated_path_flags_are_not_rebased_ambiguously(self):
        self.assertFailure('HARDSURFACE_FULL_PATH_FLAG_REQUIRED', 'hardsurface', 'run', '--requ', 'input.json')

    def test_nonexistent_and_missing_required_file(self):
        env = dict(self.env, BLENDERCTL_HARDSURFACE_ROOT=str(self.work / 'absent'))
        self.assertFailure('HARDSURFACE_DISPATCH_FAILED', 'hardsurface', 'describe', env=env)
        (self.plugin / 'hardsurface/host.py').rename(self.plugin / 'unused-host.py')
        self.assertFailure('HARDSURFACE_DISPATCH_FAILED', 'hardsurface', 'describe')

    def test_symlink_root_and_member_refused(self):
        alias = self.work / 'alias'; alias.symlink_to(self.plugin, target_is_directory=True)
        self.assertFailure('HARDSURFACE_UNSAFE_PATH', 'hardsurface', 'describe', env=dict(self.env, BLENDERCTL_HARDSURFACE_ROOT=str(alias)))
        target = self.plugin / 'hardsurface/host.py'; target.rename(self.plugin / 'host-real.py'); target.symlink_to(self.plugin / 'host-real.py')
        self.assertFailure('HARDSURFACE_UNSAFE_PATH', 'hardsurface', 'describe')

    def test_lazy_import_symlink_is_rejected(self):
        outside = self.work / 'outside.py'; outside.write_text('raise RuntimeError("MUST NOT IMPORT")')
        (self.plugin / 'hardsurface/contract.py').symlink_to(outside)
        self.assertFailure('HARDSURFACE_UNSAFE_PATH', 'hardsurface', 'describe')

    def test_importable_binary_and_sourceless_modules_rejected(self):
        for name in ['host.so', 'contract.pyc']:
            with self.subTest(name=name):
                path = self.plugin / 'hardsurface' / name
                path.write_bytes(b'fixture')
                self.assertFailure('HARDSURFACE_UNSAFE_PATH', 'hardsurface', 'describe')
                path.rename(path.with_suffix('.test-disabled'))

    def test_malformed_globals_stay_with_legacy_parser(self):
        for arguments in [['--jobs-dir', '-h', 'hardsurface'], ['--jobs-dir', '--compact', 'hardsurface'], ['--compact=no', 'hardsurface'], ['--', 'hardsurface'], ['--t', '4', 'hardsurface']]:
            self.assertIsNone(dispatcher._scan(arguments)[0], arguments)
            self.assertIsNone(dispatcher.dispatch(arguments), arguments)

    def test_manifest_and_package_version_checked_without_import(self):
        manifest = self.plugin / 'blender_manifest.toml'
        original = manifest.read_text()
        manifest.write_text(original.replace('0.2.0', '0.1.0'))
        self.assertFailure('HARDSURFACE_VERSION_MISMATCH', 'hardsurface', 'describe')
        manifest.write_text(original)
        (self.plugin / 'hardsurface/__init__.py').write_text('__version__="9.9.9"\nraise RuntimeError("MUST NOT IMPORT")\n')
        self.assertFailure('HARDSURFACE_VERSION_MISMATCH', 'hardsurface', 'describe')
        manifest.write_text(original.replace('hard_surface_workbench', 'other_plugin'))
        self.assertFailure('HARDSURFACE_INVALID_MANIFEST', 'hardsurface', 'describe')

    def test_oversized_metadata_and_nonliteral_version(self):
        init = self.plugin / 'hardsurface/__init__.py'
        init.write_text('__version__=str("0.2.0")\n')
        self.assertFailure('HARDSURFACE_DISPATCH_FAILED', 'hardsurface', 'describe')
        init.write_text('#' * (256 * 1024 + 1))
        self.assertFailure('HARDSURFACE_INVALID_METADATA', 'hardsurface', 'describe')

    def test_subprocess_exact_exit_and_isolated_imports(self):
        poison = self.work / 'poison'; poison.mkdir()
        (poison / 'hardsurface').mkdir(); (poison / 'hardsurface/__init__.py').write_text('raise RuntimeError("POISON")')
        (self.plugin / 'sitecustomize.py').write_text('raise RuntimeError("SITE MUST NOT LOAD")')
        env = dict(self.env, PYTHONPATH=str(poison), PYTHONUSERBASE=str(poison), HS_TEST_EXIT='37', BLENDER_PATH='/explicit/blender')
        result, data = self.response('hardsurface', 'describe', env=env)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(data['cwd'], str(self.plugin))
        self.assertEqual(data['path'][0], str(self.plugin))
        self.assertNotIn(str(poison), data['path'])
        self.assertTrue(data['dont_write']); self.assertTrue(data['no_site']); self.assertTrue(data['safe_path'])
        self.assertEqual(data['blender'], '/explicit/blender')
        self.assertFalse(Path(data['cache']).exists())
        self.assertFalse(list(self.plugin.rglob('*.pyc')))

    def test_global_arguments_and_relative_file_arguments(self):
        result, data = self.response('--jobs-dir=jobs', '--blender', 'runtime/blender', '--compact', '--async', 'hardsurface', 'run', '--request', 'input.json', '--reference-approval=approval.json', '--reference-approval-sha256', 'a' * 64)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data['argv'], ['--jobs-dir', str(self.work / 'jobs'), '--blender', str(self.work / 'runtime/blender'), '--compact', '--async', 'hardsurface', 'run', '--request', str(self.work / 'input.json'), '--reference-approval=' + str(self.work / 'approval.json'), '--reference-approval-sha256', 'a' * 64])

    def test_global_abbreviations_and_last_jobs_option_win(self):
        job = self.marker()
        result, data = self.response('--jobs-dir', 'ignored', '--jobs-d=' + str(job), '--comp', 'job', 'status', 'abc')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data['argv'][-3:], ['job', 'status', 'abc'])
        self.assertIn('--compact', data['argv'])

    def test_unsupported_globals_not_silently_dropped(self):
        for flags in [('--human',), ('--timeout=5',), ('--transactions-dir', 'anything')]:
            with self.subTest(flags=flags):
                self.assertFailure('HARDSURFACE_UNSUPPORTED_GLOBAL', *flags, 'hardsurface', 'describe')

    def test_literals_never_hijack_legacy_commands(self):
        self.assertEqual(dispatcher._scan(['query', 'hardsurface'])[0], 'query')
        self.assertEqual(dispatcher._scan(['--jobs-dir', 'hardsurface', 'material', 'describe'])[0], 'material')
        self.assertIsNone(dispatcher.dispatch(['query', 'hardsurface']))
        self.assertIsNone(dispatcher.dispatch(['material', 'describe', '--operation', 'hardsurface']))
        self.assertIsNone(dispatcher.dispatch(['query', '_supervise']))
        self.assertIsNone(dispatcher.dispatch(['_supervise', 'abc']))
        result = self.call('--version')
        self.assertEqual(result.returncode, 0); self.assertEqual(result.stdout.strip(), '0.55.1')
        result = self.call('--help')
        self.assertEqual(result.returncode, 0); self.assertIn('material', result.stdout); self.assertIn('hardsurface', result.stdout)
        result = self.call('material', 'describe', '--operation', 'hardsurface')
        self.assertEqual(result.returncode, 2); self.assertIn('invalid choice', result.stdout)
        self.assertNotIn('HARDSURFACE_', result.stdout)

    def test_exact_owned_job_route_without_prefix_heuristic(self):
        job = self.marker()
        result, data = self.response('--jobs-dir', str(job), 'job', 'status', 'abc')
        self.assertEqual(result.returncode, 0); self.assertEqual(data['argv'][-3:], ['job', 'status', 'abc'])
        self.assertFalse(dispatcher.owns_job_store(self.work / 'hardsurface-unmarked'))
        ordinary = self.marker('ordinary', kind='other_job_store')
        result = self.call('--jobs-dir', str(ordinary), 'job', 'status', 'abc')
        self.assertNotIn('"argv"', result.stdout)
        result = self.call('--jobs-dir', str(self.work / 'hardsurface-unmarked'), 'job', 'status', 'abc')
        self.assertNotEqual(result.returncode, 0); self.assertNotIn('"argv"', result.stdout)
        self.assertFalse((self.work / 'hardsurface-unmarked').exists())

    def test_marker_mismatch_duplicate_and_symlink_rejected(self):
        job = self.marker(root='/some/other/root')
        self.assertFailure('HARDSURFACE_JOB_OWNER_MISMATCH', '--jobs-dir', str(job), 'job', 'status', 'abc')
        (job / 'owner.json').write_text('{"kind":"hardsurface_job_store","kind":"hardsurface_job_store"}')
        self.assertFailure('HARDSURFACE_DISPATCH_FAILED', '--jobs-dir', str(job), 'job', 'status', 'abc')
        (job / 'owner.json').rename(job / 'real-owner.json'); (job / 'owner.json').symlink_to(job / 'real-owner.json')
        self.assertFailure('HARDSURFACE_UNSAFE_PATH', '--jobs-dir', str(job), 'job', 'status', 'abc')

    def test_jobs_environment_and_relative_store(self):
        job = self.marker()
        result, data = self.response('job', 'status', 'abc', env=dict(self.env, BLENDERCTL_JOBS_DIR=str(job)))
        self.assertEqual(result.returncode, 0); self.assertEqual(data['argv'][:2], ['--jobs-dir', str(job)])
        result, data = self.response('--jobs-dir', job.name, 'job', 'recover', 'request-1')
        self.assertEqual(result.returncode, 0); self.assertEqual(data['argv'][:2], ['--jobs-dir', str(job)])
        result, data = self.response('hardsurface', 'describe', env=dict(self.env, BLENDERCTL_JOBS_DIR=str(job)))
        self.assertEqual(data['argv'][:2], ['--jobs-dir', str(job)])

    def test_failed_launch_is_structured(self):
        sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
        try:
            output = io.StringIO()
            with patch.dict(os.environ, self.env, clear=True), patch.object(dispatcher.subprocess, 'run', side_effect=OSError('fixture launch denied')), contextlib.redirect_stdout(output):
                code = dispatcher.dispatch(['hardsurface', 'describe'])
            self.assertEqual(code, 2); self.assertEqual(json.loads(output.getvalue())['error']['detail_code'], 'HARDSURFACE_DISPATCH_FAILED')
        finally:
            sys.path.pop(0)


if __name__ == '__main__':
    unittest.main()
