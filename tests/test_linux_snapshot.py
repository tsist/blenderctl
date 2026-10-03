# SPDX-License-Identifier: GPL-3.0-or-later
"""Linux snapshot import/archive tests; optional real Blender roundtrip.

BLENDERCTL_TEST_BLENDER=/absolute/blender python -m unittest discover -s tests -p test_linux_snapshot.py
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('linux_snapshot', ROOT / 'scripts/linux_snapshot.py')
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def descriptor(path):
    return {'file': str(path), 'expected_sha256': sha(path)}


def request_for(source, texture):
    resource = descriptor(texture)
    return {'schema_version': '1.0', 'command': 'material.run', 'params': {
        **descriptor(source), 'resources': [resource],
        'manifest': {'schema_version': '1.1', 'context': {'scene': 'Scene', 'view_layer': 'ViewLayer', 'frame': 1},
                     'target': {'object': 'Cube', 'uv_layer': 'UVMap'},
                     'material': {'id': 'SnapshotMaterial', 'name': 'Snapshot Material', 'template': 'pbr_layers_v2'},
                     'layers': [{'id': 'Base', 'name': 'Base', 'channels': {'base_color': dict(resource, color_space='sRGB')},
                                 'values': {'roughness': 0.45}}],
                     'preview': {'engine': 'CYCLES', 'device': {'backend': 'CPU'}, 'width': 64, 'height': 64,
                                 'samples': 1, 'denoise': False, 'views': [{'id': 'Front', 'azimuth': 45, 'elevation': 20}]}}}}


class FakeGuard:
    """Only pure unit tests use this; native tests exercise actual Linux guards."""
    def __init__(self, path, **kwargs): self.path = Path(path)
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def sha256(self): return sha(self.path)
    def chunks(self): yield self.path.read_bytes()


@unittest.skipUnless(os.name == 'posix', 'Linux paths required')
class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='blenderctl-snapshot-unit-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.blend'; self.source.write_bytes(b'BLENDER-v052fake')
        self.texture = self.root / 'texture.png'; self.texture.write_bytes(b'png fixture')
        self.blender = self.root / 'blender'; self.blender.write_bytes(b'fake binary')
        self.request = self.root / 'request.json'
        write_json(self.request, request_for(self.source, self.texture))

    def tearDown(self): self.temp.cleanup()

    def worker(self, command, **kwargs):
        self.assertIn('--factory-startup', command)
        self.assertIn('--disable-autoexec', command)
        self.assertIn('--offline-mode', command)
        plan = json.loads(Path(command[-1]).read_text())
        self.assertEqual(plan['original_source'], str(self.source))
        Path(plan['snapshot']).write_bytes(b'rebased snapshot')
        write_json(Path(command[-1]).parent / 'worker-report.json', {'ok': True, 'rebound': [], 'snapshot_sha256': sha(plan['snapshot'])})
        return subprocess.CompletedProcess(command, 0)

    def stage(self, destination=None):
        with patch('filesystem.FileGuard', FakeGuard), patch.object(snapshot.subprocess, 'run', self.worker):
            return snapshot.stage(self.request, destination or self.root / 'staged', self.blender)

    def test_stage_explicit_request_and_rebased_descriptors(self):
        before = sha(self.source)
        report = self.stage()
        result = json.loads(Path(report['request']).read_text())
        self.assertEqual(result['command'], 'material.run')
        self.assertEqual(sha(self.source), before)
        self.assertFalse(report['original_source_mutation_prevented'])
        self.assertNotEqual(result['params']['file'], str(self.source))
        self.assertEqual(result['params']['expected_sha256'], sha(result['params']['file']))
        resource = result['params']['resources'][0]
        image = result['params']['manifest']['layers'][0]['channels']['base_color']
        self.assertEqual(image['file'], resource['file'])
        self.assertTrue(Path(resource['file']).is_relative_to(self.root / 'staged'))
        self.assertEqual(resource['expected_sha256'], sha(self.texture))

    def test_worker_rejects_mismatched_source_version(self):
        fake_bpy = SimpleNamespace(
            ops=SimpleNamespace(wm=SimpleNamespace(open_mainfile=lambda **kwargs: None)),
            data=SimpleNamespace(version=(4, 3, 0)), app=SimpleNamespace(version=(5, 2, 2)))
        worker_spec = importlib.util.spec_from_file_location('snapshot_worker_unit', ROOT / 'tools/blenderctl/linux_snapshot_worker.py')
        worker = importlib.util.module_from_spec(worker_spec)
        with patch.dict('sys.modules', {'bpy': fake_bpy}):
            worker_spec.loader.exec_module(worker)
        with patch.object(worker, 'FileGuard', FakeGuard), self.assertRaisesRegex(snapshot.Failure, 'matching Blender major/minor'):
            worker.run({'source_copy': descriptor(self.source), 'snapshot': str(self.root / 'other.blend'),
                        'original_source': str(self.source), 'resources': {}})

    def test_template_expanded_and_bindings_rebased(self):
        from material_workflow_addon.contract import normalize_manifest
        from material_workflow_addon.templates import template_from_manifest
        raw = json.loads(self.request.read_text())
        manifest = normalize_manifest(raw['params']['manifest'])
        template = template_from_manifest(manifest, 'SnapshotTemplate', 'Snapshot Template', '1.0.0')
        bindings = {k: manifest[k] for k in ('context', 'target', 'preview')}
        bindings.update(material={k: manifest['material'][k] for k in ('id', 'name')},
                        resources={'Base_base_color': manifest['layers'][0]['channels']['base_color']}, groups={})
        for key, value in [('template', template), ('bindings', bindings)]:
            path = self.root / (key + '.json'); write_json(path, value)
            raw['params'][key] = descriptor(path)
        del raw['params']['manifest']; write_json(self.request, raw)
        report = self.stage()
        request = json.loads(Path(report['request']).read_text())
        self.assertTrue(report['template_expanded'])
        self.assertNotIn('template', request['params'])
        self.assertNotIn('bindings', request['params'])
        self.assertIn('manifest', request['params'])

    def test_mismatch_never_launches_blender(self):
        self.texture.write_bytes(b'changed')
        with patch('filesystem.FileGuard', FakeGuard), patch.object(snapshot.subprocess, 'run') as runner:
            with self.assertRaisesRegex(snapshot.Failure, 'SHA256'):
                snapshot.stage(self.request, self.root / 'staged', self.blender)
            runner.assert_not_called()

    def assert_growth_stays_within_import_budget(self, growing, destination, budget):
        original_read = os.read
        growing_identity = (growing.stat().st_dev, growing.stat().st_ino)
        appended = False

        def read_then_grow(fd, size):
            nonlocal appended
            block = original_read(fd, size)
            info = os.fstat(fd)
            if block and not appended and (info.st_dev, info.st_ino) == growing_identity:
                # Append after preflight and the copy's initial fstat, while its
                # first block is in flight. The NEXT block must not be written.
                with growing.open('ab') as stream:
                    stream.write(b'growth exceeds remaining cumulative budget')
                appended = True
            return block

        with patch.object(snapshot, 'MAX_TOTAL', budget), patch('filesystem.FileGuard', FakeGuard), \
                patch.object(snapshot.os, 'read', side_effect=read_then_grow), \
                patch.object(snapshot.subprocess, 'run') as runner:
            with self.assertRaisesRegex(snapshot.Failure, 'grew beyond bounded import size limit') as caught:
                snapshot.stage(self.request, destination, self.blender)
            self.assertEqual(caught.exception.code, 'RESOURCE_LIMIT')
            runner.assert_not_called()
        self.assertTrue(appended)
        self.assertTrue(destination.is_dir(), 'Failed partial destination must remain reviewable')
        imported = [p for name in ('inputs', 'resources') for p in (destination / name).rglob('*') if p.is_file()]
        self.assertLessEqual(sum(p.stat().st_size for p in imported), budget)
        self.assertFalse((destination / 'request.json').exists())
        return imported

    def test_cumulative_budget_stops_growing_later_resource_before_block_write(self):
        second = self.root / 'second.png'; second.write_bytes(b'small')
        raw = json.loads(self.request.read_text())
        raw['params']['resources'].append(descriptor(second)); write_json(self.request, raw)
        initial_total = sum(p.stat().st_size for p in (self.source, self.texture, second))
        imported = self.assert_growth_stays_within_import_budget(second, self.root / 'staged', initial_total + 3)
        self.assertEqual(sum(p.stat().st_size for p in imported), initial_total)
        self.assertEqual(len(imported), 3, 'Two completed files and the bounded partial resource remain')

    def test_cumulative_budget_also_applies_to_growing_source_copy(self):
        initial_source = self.source.stat().st_size
        budget = initial_source + self.texture.stat().st_size + 3
        imported = self.assert_growth_stays_within_import_budget(self.source, self.root / 'staged', budget)
        self.assertEqual(sum(p.stat().st_size for p in imported), initial_source)
        self.assertEqual(len(imported), 1)

    def test_symlink_input_and_parent_are_rejected(self):
        alias = self.root / 'alias'; alias.symlink_to(self.texture)
        with self.assertRaises(snapshot.Failure): snapshot.read_bytes(alias)
        link = self.root / 'linked'; link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(snapshot.Failure): snapshot.read_bytes(link / self.texture.name)

    def test_hardlink_input_is_rejected(self):
        os.link(self.texture, self.root / 'hardlink.png')
        with self.assertRaises(snapshot.Failure): self.stage()

    def test_existing_destination_untouched(self):
        destination = self.root / 'staged'; destination.mkdir()
        witness = destination / 'keep'; witness.write_text('keep')
        with self.assertRaises(snapshot.Failure): self.stage(destination)
        self.assertEqual(witness.read_text(), 'keep')

    def test_malformed_paths_and_envelope(self):
        for value in ['relative.blend', '/tmp/../tmp/source.blend', '//tmp/source.blend', '/tmp//file', '/tmp/file\\path', '/tmp/x\x00']:
            with self.assertRaises(snapshot.Failure): snapshot.absolute_path(value)
        for value in [{'file': str(self.source)}, {'schema_version': '1.0', 'command': 'inspect', 'params': {}}]:
            with self.assertRaises(snapshot.Failure): snapshot.material_request(value)
        with self.assertRaises(snapshot.Failure): snapshot.parse_json_bytes(b'{"x":1,"x":2}')

    def test_undeclared_manifest_resource_rejected(self):
        raw = json.loads(self.request.read_text())
        raw['params']['resources'] = []; write_json(self.request, raw)
        with self.assertRaisesRegex(snapshot.Failure, 'Undeclared'):
            self.stage()

    def make_job(self):
        job = self.root / ('a' * 32); job.mkdir()
        (job / 'textures').mkdir(); (job / 'views').mkdir()
        (job / 'textures' / 'texture.png').write_bytes(b'image bytes')
        (job / 'candidate.blend').write_bytes(b'candidate //textures/texture.png')
        (job / 'views' / 'preview.png').write_bytes(b'preview')
        write_json(job / 'status.json', {'job_id': job.name, 'state': 'succeeded', 'exit_code': 0})
        write_json(job / 'result.json', {'schema_version': '1.0', 'ok': True, 'error': None,
                                       'command': 'material.run', 'job_id': job.name,
                                       'data': {'candidate': str(job / 'candidate.blend'),
                                                'candidate_sha256': sha(job / 'candidate.blend'),
                                                'resources': [descriptor(job / 'textures' / 'texture.png')]}})
        return job

    def test_archive_preserves_entire_job_and_byte_hashes(self):
        job = self.make_job(); dest = self.root / 'archive'
        with patch('filesystem.FileGuard', FakeGuard): result = snapshot.export_job(job, dest)
        self.assertEqual(result['candidate'], job.name + '/candidate.blend')
        self.assertEqual(len(result['files']), 5)
        for row in result['files']:
            self.assertEqual(sha(dest / row['path']), row['sha256'])
        self.assertTrue((dest / job.name / 'textures' / 'texture.png').exists())
        self.assertTrue((job / 'candidate.blend').exists())

    def test_archive_rejects_nonterminal_or_failed_authoritative_results(self):
        for index, change in enumerate([{'state': 'running'}, {'exit_code': 5}]):
            job = self.make_job() if index == 0 else self.root / ('a' * 32)
            state = {'job_id': job.name, 'state': 'succeeded', 'exit_code': 0, **change}
            write_json(job / 'status.json', state)
            with patch('filesystem.FileGuard', FakeGuard), self.assertRaises(snapshot.Failure):
                snapshot.export_job(job, self.root / ('archive' + str(index)))
            self.assertFalse((self.root / ('archive' + str(index))).exists())

    def test_archive_rejects_tamper_and_symlinks(self):
        job = self.make_job()
        (job / 'candidate.blend').write_bytes(b'tampered')
        with patch('filesystem.FileGuard', FakeGuard), self.assertRaises(snapshot.Failure):
            snapshot.export_job(job, self.root / 'archive')
        (job / 'symlink').symlink_to(self.texture)
        with self.assertRaises(snapshot.Failure): snapshot.inventory(job)


@unittest.skipUnless(os.environ.get('BLENDERCTL_TEST_BLENDER'), 'Set BLENDERCTL_TEST_BLENDER for native Blender tests')
class NativeSnapshotTests(unittest.TestCase):
    def test_rejects_dynamic_and_undeclared_native_dependencies(self):
        blender = Path(os.environ['BLENDERCTL_TEST_BLENDER'])
        worker = ROOT / 'tests/linux_snapshot_native.py'
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='blenderctl-snapshot-negative-') as temp:
            root = Path(temp)
            for mode, phrase in [('fixture-driver', 'Drivers'), ('fixture-sequence', 'static FILE'), ('fixture', 'Undeclared')]:
                with self.subTest(mode=mode):
                    source = root / mode; source.mkdir()
                    subprocess.run([str(blender), '-b', '--factory-startup', '--disable-autoexec', '--offline-mode',
                                    '--python-exit-code', '1', '--python', str(worker), '--', mode, str(source), str(ROOT)],
                                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                    if mode == 'fixture':
                        request = json.loads((source / 'request.json').read_text())
                        request['params']['resources'] = [r for r in request['params']['resources'] if 'RelativeTexture' not in r['file']]
                        write_json(source / 'request.json', request)
                    before = sha(source / 'source.blend')
                    with self.assertRaisesRegex(snapshot.Failure, phrase):
                        snapshot.stage(source / 'request.json', root / (mode + '-snapshot'), blender)
                    self.assertEqual(sha(source / 'source.blend'), before)

    def test_absolute_relative_texture_and_font_roundtrip(self):
        blender = Path(os.environ['BLENDERCTL_TEST_BLENDER'])
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='blenderctl-snapshot-native-') as temp:
            root = Path(temp); source = root / 'source'; source.mkdir()
            worker = ROOT / 'tests/linux_snapshot_native.py'
            subprocess.run([str(blender), '-b', '--factory-startup', '--disable-autoexec', '--offline-mode',
                            '--python-exit-code', '1', '--python', str(worker), '--', 'fixture', str(source), str(ROOT)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            before = sha(source / 'source.blend')
            report = snapshot.stage(source / 'request.json', root / 'snapshot', blender)
            self.assertEqual(sha(source / 'source.blend'), before)
            self.assertGreaterEqual(len(report['rebound']), 2)
            self.assertEqual({row['name'] for row in report['rebound'] if row['kind'] == 'Image'}, {'AbsoluteTexture', 'RelativeTexture'})
            subprocess.run([str(blender), '-b', '--factory-startup', '--disable-autoexec', '--offline-mode',
                            '--python-exit-code', '1', '--python', str(worker), '--', 'oracle', str(root / 'snapshot'), str(ROOT)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)


if __name__ == '__main__': unittest.main()
