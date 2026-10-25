"""Public procedural-material host routing and rejection gates; no Blender launch.

The shared-contract forwarding tests deliberately use an isolated stub. Actual
manifest semantics and visual/bake acceptance belong to the Blender fixtures.
"""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import execution_contract
import material_interface
import material_procedural_contract as contract
import protocol
import runner
from pipeline_contract import ALLOWED


def shared_stub():
    return types.SimpleNamespace(
        __file__=__file__, MANIFEST_SCHEMA={'type': 'object', 'properties': {
            'schema_version': {'const': '2.0'}, 'context': contract.CONTEXT,
            'target': contract.TARGET, 'signals': {'type': 'array'}}},
        EXPORT_SCHEMA={'type': 'object', 'properties': {'size': {'type': 'integer', 'default': 2048}},
                       'additionalProperties': False},
        normalize_params=Mock(return_value={'normalized': True}),
        normalize_export=Mock(side_effect=lambda value: copy.deepcopy(value)))


class ProceduralRoutes(unittest.TestCase):
    def real_run_params(self):
        return {'file': str(Path('source.blend').resolve()), 'expected_sha256': 'a' * 64, 'resources': [],
                'manifest': {'schema_version': '2.0',
                    'context': {'scene': 'Scene', 'view_layer': 'ViewLayer'},
                    'target': {'object': 'Crate', 'uv_layer': 'UVMap'},
                    'material': {'id': 'mat_test', 'name': 'Test Material'},
                    'signals': [{'id': 'base_noise', 'type': 'noise', 'parameters': {}, 'inputs': {}}],
                    'layers': [{'id': 'base', 'name': 'Base'}],
                    'preview': {'device': {'backend': 'CPU'}, 'views': [{'id': 'front'}]}}}

    def test_real_shared_manifest_and_optional_export_defaults(self):
        params = self.real_run_params()
        params['export'] = {'size': 64}
        normalized = protocol.validate({'schema_version': '1.0', 'command': 'material.procedural-run',
                                        'params': params})['params']
        self.assertEqual(normalized['manifest']['schema_version'], '2.0')
        self.assertEqual(normalized['manifest']['context']['frame'], 1)
        self.assertEqual(normalized['manifest']['preview']['width'], 384)
        self.assertEqual(normalized['export']['channels'], ['base_color', 'roughness', 'metallic', 'normal'])
        self.assertNotIn('frame', params['manifest']['context'])

    def test_real_shared_contract_rejects_unknown_procedure_and_output_location(self):
        bad = []
        params = self.real_run_params(); params['manifest']['signals'][0]['type'] = 'python'; bad.append(params)
        params = self.real_run_params(); params['export'] = {'output_dir': 'unowned'}; bad.append(params)
        params = self.real_run_params(); params['export'] = {'size': 4096}; bad.append(params)
        params = self.real_run_params(); params['manifest']['target']['material_slot'] = -1; bad.append(params)
        params = self.real_run_params(); params['manifest']['layers'][0]['channels'] = {'roughness': 'missing'}; bad.append(params)
        params = self.real_run_params(); params['resources'] = None; bad.append(params)
        for params in bad:
            with self.subTest(params=params), self.assertRaises(protocol.Failure) as caught:
                contract.normalize_params(params)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_operations_are_unique_standalone_and_not_dag(self):
        exclusions = execution_contract.capabilities()['pipeline_exclusions']
        for command in contract.COMMANDS:
            self.assertEqual(protocol.COMMANDS.count(command), 1)
            self.assertNotIn(command, protocol.HOST_COMMANDS)
            self.assertNotIn(command, ALLOWED)
            self.assertEqual([r['classification'] for r in exclusions if r['command'] == command],
                             ['standalone_workflow'])

    def test_run_strips_only_cli_export_before_shared_normalization(self):
        shared = shared_stub()
        params = {'file': 'source', 'manifest': {'schema_version': '2.0'}, 'export': {'size': 256}}
        original = copy.deepcopy(params)
        with patch.object(contract, 'shared_contract', return_value=shared):
            normalized = contract.normalize_params(params)
        shared.normalize_params.assert_called_once_with({'file': 'source', 'manifest': {'schema_version': '2.0'}})
        shared.normalize_export.assert_called_once_with({'size': 256})
        self.assertEqual(normalized, {'normalized': True, 'export': {'size': 256}})
        self.assertEqual(params, original)

    def test_null_export_is_optional_without_calling_settings_normalizer(self):
        shared = shared_stub()
        with patch.object(contract, 'shared_contract', return_value=shared):
            normalized = contract.normalize_params({'export': None})
        self.assertIsNone(normalized['export'])
        shared.normalize_export.assert_not_called()

    def export_params(self):
        return {'file': str(Path('source.blend').resolve()), 'expected_sha256': 'a' * 64,
                'resources': [], 'context': {'scene': 'Scene', 'view_layer': 'ViewLayer'},
                'target': {'object': 'Cube', 'uv_layer': 'UVMap'}, 'export': {'size': 256}}

    def test_export_envelope_defaults_and_shared_settings(self):
        shared = shared_stub()
        params = self.export_params()
        with patch.object(contract, 'shared_contract', return_value=shared):
            normalized = contract.normalize_export_params(params)
        self.assertEqual(normalized['context']['frame'], 1)
        self.assertEqual(normalized['target']['material_slot'], 0)
        self.assertNotIn('frame', params['context'])
        shared.normalize_export.assert_called_once_with({'size': 256})

    def test_export_rejects_unknown_fields_missing_inputs_and_arbitrary_output(self):
        bad = []
        for field in ('file', 'expected_sha256', 'resources', 'context', 'target', 'export'):
            params = self.export_params()
            del params[field]
            bad.append(params)
        for extra in ({'output_dir': str(Path('unowned').resolve())}, {'manifest': {}}, {'expected_sha256': 'A' * 64},
                      {'file': 'relative.blend'}, {'file': str(Path('source.txt').resolve())}, {'export': None},
                      {'context': {'scene': 'Scene', 'view_layer': 'ViewLayer', 'frame': True}},
                      {'target': {'object': 'Cube', 'uv_layer': 'UVMap', 'material_slot': -1}}):
            params = self.export_params()
            params.update(extra)
            bad.append(params)
        shared = shared_stub()
        with patch.object(contract, 'shared_contract', return_value=shared):
            for params in bad:
                with self.subTest(params=params), self.assertRaises(protocol.Failure) as caught:
                    contract.normalize_export_params(params)
                self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_duplicate_export_resource_paths_rejected(self):
        params = self.export_params()
        item = {'file': str(Path('image.png').resolve()), 'expected_sha256': 'b' * 64}
        params['resources'] = [item, copy.deepcopy(item)]
        with self.assertRaises(protocol.Failure) as caught:
            contract.normalize_export_params(params)
        self.assertEqual(caught.exception.code, 'INVALID_REQUEST')

    def test_discovery_returns_host_only_versioned_inline_schema(self):
        shared = shared_stub()
        with patch.object(contract, 'shared_contract', return_value=shared):
            value = material_interface.describe('procedural-run', include_schema=True)
            row = value['data']['operations'][0]
            section = material_interface.describe('procedural-run', section='signals')['data']['operations'][0]
            export = material_interface.describe('procedural-export', section='export')['data']['operations'][0]
        self.assertEqual(row['command'], 'material.procedural-run')
        self.assertEqual(row['schema']['properties']['params']['properties']['manifest'], shared.MANIFEST_SCHEMA)
        self.assertEqual(row['request_schema']['kind'], 'inline')
        self.assertEqual(row['request_schema']['contract']['sha256'], protocol.digest(Path(contract.__file__)))
        self.assertEqual(section['schema'], {'type': 'array'})
        self.assertEqual(export['schema'], shared.EXPORT_SCHEMA)

    def test_protocol_dispatches_both_public_contracts(self):
        for command, function in (('material.procedural-run', 'normalize_params'),
                                  ('material.procedural-export', 'normalize_export_params')):
            with self.subTest(command=command), patch.object(contract, function, return_value={'checked': command}) as normalize:
                result = protocol.validate({'schema_version': '1.0', 'command': command, 'params': {'input': 1}})
            normalize.assert_called_once_with({'input': 1})
            self.assertEqual(result['params'], {'checked': command})

    def test_cli_parser_requires_explicit_resources_and_export_context(self):
        for action in ('procedural-run', 'procedural-export'):
            with self.subTest(action=action), self.assertRaises(protocol.Failure):
                cli.parser().parse_args(['material', action, 'source.blend', '--expected-sha256', 'a' * 64])
        args = cli.parser().parse_args(['--async', 'material', 'procedural-export', 'source.blend',
            '--expected-sha256', 'a' * 64, '--resources', 'r.json', '--context', 'c.json',
            '--target', 't.json', '--export', 'e.json'])
        self.assertTrue(args.background)
        self.assertFalse(hasattr(args, 'output'))

    def test_cli_serializes_run_and_export_without_user_output_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = {'manifest': {'schema_version': '2.0'}, 'resources': [],
                         'context': {'scene': 'Scene'}, 'target': {'object': 'Cube'}, 'export': {'size': 256}}
            for key, data in documents.items():
                protocol.atomic_json(root / (key + '.json'), data)
            for action, keys in (('procedural-run', ('manifest', 'resources', 'export')),
                                 ('procedural-export', ('resources', 'context', 'target', 'export'))):
                args = ['--async', 'material', action, 'source.blend', '--expected-sha256', 'b' * 64]
                for key in keys:
                    args += ['--' + key, str(root / (key + '.json'))]
                with patch.object(cli.runner, 'prepare', return_value=root) as prepare, \
                     patch.object(cli.runner, 'submit', return_value=protocol.envelope('job.submit', 'a' * 32, {})), \
                     contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(args)
                self.assertEqual(code, 0)
                request = prepare.call_args.args[0]
                self.assertEqual(request['command'], 'material.' + action)
                self.assertEqual(request['params'], {'file': 'source.blend', 'expected_sha256': 'b' * 64,
                                                     **{key: documents[key] for key in keys}})

    def test_persisted_export_evidence_survives_compact_failure_and_cancel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protocol.atomic_json(root / 'request.json', {'command': 'material.procedural-export', 'params': {
                'file': 'source.blend', 'expected_sha256': 'a' * 64}})
            protocol.atomic_json(root / 'procedural-report.json', {'status': 'partial', 'checks': {},
                'author': {'file': 'author.blend', 'expected_sha256': 'b' * 64},
                'reconstructed': {'file': 'reconstructed.blend', 'expected_sha256': 'c' * 64},
                'outputs': [{'id': 'reconstructed_hero', 'status': 'failed', 'error': 'render failed'}],
                'export': {'status': 'complete', 'channels': {'roughness': {
                    'file': 'rough.png', 'sha256': 'd' * 64, 'width': 2048, 'height': 2048,
                    'pixel_stats': {'do_not_repeat': [1] * 100}}}}})
            result = protocol.envelope('job.status', 'a' * 32, {'state': 'cancelled'},
                                       artifacts={'job_directory': str(root)})
            compact = material_interface.compact(result)
        self.assertEqual(compact['data']['status'], 'cancelled')
        self.assertEqual(compact['data']['persisted_report_status'], 'partial')
        self.assertEqual(compact['data']['export']['channels']['roughness']['width'], 2048)
        self.assertNotIn('pixel_stats', compact['data']['export']['channels']['roughness'])
        self.assertEqual(compact['data']['failures']['outputs'][0]['error'], 'render failed')
        self.assertEqual(compact['data']['author']['expected_sha256'], 'b' * 64)
        self.assertIn('report', compact['artifacts'])

    def test_worker_dispatch_is_owned_material_adapter(self):
        tree = ast.parse(Path(cli.__file__).with_name('worker.py').read_text(encoding='utf-8'))
        branches = [node for node in ast.walk(tree) if isinstance(node, ast.If) and
                    ast.unparse(node.test) == "command in ('material.procedural-run', 'material.procedural-export')"]
        self.assertEqual(len(branches), 1)
        text = '\n'.join(ast.unparse(node) for node in branches[0].body)
        self.assertIn('material_procedural.run', text)
        self.assertIn('material_procedural.export', text)

    def test_supervisor_rejects_source_and_resource_sha_without_worker(self):
        for command in contract.COMMANDS:
            for bad_source in (True, False):
                with self.subTest(command=command, bad_source=bad_source), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, resource = root / 'source.blend', root / 'resource.png'
                    source.write_bytes(b'guard test only, never opened by Blender')
                    resource.write_bytes(b'hashed resource')
                    params = {'file': str(source),
                              'expected_sha256': 'a' * 64 if bad_source else protocol.digest(source),
                              'resources': [{'file': str(resource), 'expected_sha256': 'b' * 64}]}
                    job = root / uuid.uuid4().hex
                    job.mkdir()
                    protocol.atomic_json(job / 'request.json', {'schema_version': '1.0', 'command': command, 'params': params})
                    protocol.atomic_json(job / 'launch.json', {'blender': sys.executable, 'timeout_seconds': 30,
                                                              'transactions_root': str(root / 'transactions')})
                    protocol.atomic_json(job / 'status.json', {'job_id': job.name, 'state': 'queued',
                                                              'heartbeat_at': time.time()})
                    before = source.read_bytes(), resource.read_bytes()
                    with patch.object(runner, 'ProcessTree') as process:
                        result, code = runner.execute_worker(job)
                    process.assert_not_called()
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error']['code'], 'CONFLICT')
                    self.assertEqual(code, protocol.CODES['CONFLICT'])
                    self.assertEqual((source.read_bytes(), resource.read_bytes()), before)

    def test_progress_visible_without_worker_or_repeated_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protocol.atomic_json(root / 'status.json', {'state': 'running', 'heartbeat_at': time.time()})
            protocol.atomic_json(root / 'procedural-progress.json', {'stage': 'baking', 'completed_views': 2,
                'latest_successful_image': {'file': 'latest.png', 'sha256': 'c' * 64}})
            observed = runner.status(root)
        self.assertEqual(observed['data']['progress']['stage'], 'baking')
        self.assertEqual(observed['data']['progress']['completed_views'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
