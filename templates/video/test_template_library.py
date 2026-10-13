"""Pure compiler fixtures; tiny non-media files and no process/render calls."""
import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
sys.pycache_prefix = str(Path(__file__).resolve().parent / ('_absent_test_pycache_' + uuid.uuid4().hex))
import template_library as lib


class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The public compiler sits inside the protected runtime tree; fixtures must be external.
        # Windows TEMP may use an 8.3 alias; match the contract's canonical paths.
        cls.root = Path(tempfile.mkdtemp(prefix='video-template-pure-')).resolve()
        cls.source = cls.root / 'source.blend'
        cls.source.write_bytes(b'not Blender: pure compiler binding fixture\n')
        cls.font = cls.root / 'font.ttf'
        cls.font.write_bytes(b'not a font: pure binding fixture\n')
        cls.image = cls.root / 'image.png'
        cls.image.write_bytes(b'not an image: pure binding fixture\n')
        cls.sound = cls.root / 'sound.wav'
        cls.sound.write_bytes(b'not sound: pure binding fixture\n')
        cls.original = {str(p): lib.sha256_file(p) for p in (cls.source, cls.font, cls.image, cls.sound)}
        cls.template, cls.template_desc = lib.load_template('cg-clean')
        cls.executed_names = []

    @classmethod
    def tearDownClass(cls):
        for path, digest in cls.original.items():
            assert lib.sha256_file(path) == digest
        summary = {'status': 'passed' if cls.result.wasSuccessful() else 'failed',
                   'tests_run': cls.result.testsRun, 'failures': len(cls.result.failures),
                   'errors': len(cls.result.errors), 'scope': 'pure compiler; synthetic tiny source bytes; native execution not_run',
                   'test_names': cls.executed_names,
                   'compiler_sources': [{'file': str(lib.ROOT / name), 'expected_sha256': lib.sha256_file(lib.ROOT / name)}
                                        for name in ('template_library.py', 'cli.py', 'test_template_library.py')],
                   'fixture_root': str(cls.root), 'source_protection': cls.original,
                   'runtime_implementation_sha256': lib._runtime_identity(),
                   'runtime_bytecode_prefix_absent': not Path(sys.pycache_prefix).exists()}
        (cls.root / 'RESULT.json').write_bytes(lib.canonical_bytes(summary))

    def run(self, result=None):
        type(self).result = result
        # unittest invokes run before the first setUpClass; keep list initialized
        # by setUpClass and append the name from setUp instead.
        return super().run(result)

    def setUp(self):
        type(self).executed_names.append(self._testMethodName)

    @classmethod
    def descriptor(cls, path):
        return {'file': str(path), 'expected_sha256': lib.sha256_file(path)}

    def recipe(self):
        return {'context': {**self.descriptor(self.source), 'resources': []},
                'parameters': {'workflow_id': 'fixture', 'scene': 'Fixture', 'fps': [24, 1], 'width': 320, 'height': 180,
                               'frame_start': 7, 'shots': [{'id': 'A', 'kind': 'COLOR', 'duration': 12, 'color': [0.2, 0.3, 0.4, 1]},
                                                        {'id': 'B', 'kind': 'COLOR', 'duration': 12, 'color': [0.5, 0.4, 0.3, 1]}]}}

    def recipe_file(self, recipe, name=None):
        path = self.root / (name or ('recipe-' + uuid.uuid4().hex + '.json'))
        path.write_bytes(lib.canonical_bytes(recipe))
        return path

    def assert_rejected(self, template=None, recipe=None, code=None):
        with self.assertRaises(lib.TemplateError) as caught:
            lib.compile_recipe(template or self.template, recipe or self.recipe())
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_sequence_deterministic_half_open(self):
        params = lib.compile_recipe(self.template, self.recipe())
        self.assertEqual(params, lib.compile_recipe(self.template, self.recipe()))
        self.assertEqual(params['manifest']['timeline']['frame_end_exclusive'], 31)
        shots = [r for r in params['manifest']['clips'] if r['channel'] == 2]
        self.assertEqual([(r['shot_id'], r['start'], r['duration']) for r in shots], [('A', 7, 12), ('B', 19, 12)])
        self.assertEqual(params['implementation_sha256'], lib.RUNTIME_SHA256)

    def test_builtin_catalog_all_pinned(self):
        catalog = lib.load_catalog()
        self.assertEqual({r['id'] for r in catalog['templates']}, {'whitebox-board', 'ai-final', 'cg-clean', 'subtitle-readable'})
        for row in catalog['templates']:
            template, _ = lib.load_template(row['id'])
            recipe = self.recipe()
            recipe['parameters']['font'] = self.descriptor(self.font)
            lib.compile_recipe(template, recipe)

    def test_generated_text_timing_resources(self):
        template = copy.deepcopy(self.template)
        template['style']['show_shot_labels'] = True
        recipe = self.recipe()
        recipe['parameters'].update(font=self.descriptor(self.font), title='标题', title_duration=8,
                                     subtitles=[{'id': 'sub', 'offset': 2, 'duration': 6, 'text': '中文字幕'}])
        params = lib.compile_recipe(template, recipe)
        texts = [r for r in params['manifest']['clips'] if r['kind'] == 'TEXT']
        self.assertEqual(len(texts), 4)
        subtitle = next(r for r in texts if r['text'] == '中文字幕')
        self.assertEqual((subtitle['channel'], subtitle['start'], subtitle['duration']), (30, 9, 6))
        self.assertEqual(subtitle['font_size'], round(180 * template['style']['subtitle']['font_ratio']))
        self.assertEqual(params['resources'], [self.descriptor(self.font)])

    def test_candidates_preserved_resource_closure(self):
        recipe = self.recipe()
        desc = self.descriptor(self.image)
        recipe['parameters']['shots'][0] = {'id': 'A', 'kind': 'IMAGE', 'duration': 12, 'candidate_id': 'picked',
                                          'source': {'files': [desc]}, 'alternatives': [{'candidate_id': 'alternate', 'source': {'files': [desc]}}]}
        recipe['context']['resources'] = [desc, desc]
        params = lib.compile_recipe(self.template, recipe)
        self.assertEqual(params['resources'], [desc])
        shot = params['manifest']['clips'][1]
        self.assertEqual(shot['candidate_id'], 'picked')
        self.assertEqual(shot['alternatives'][0]['candidate_id'], 'alternate')
        self.assertEqual(shot['shot_id'], 'A')

    def test_audio_unity_speed_allowed(self):
        recipe = self.recipe()
        recipe['parameters']['audio_tracks'] = [{'id': 'dialogue', 'offset': 0, 'duration': 24,
                                                 'source': {'files': [self.descriptor(self.sound)], 'speed': 1},
                                                 'volume': 0.7, 'fade_in': 2, 'fade_out': 3}]
        params = lib.compile_recipe(self.template, recipe)
        audio = next(r for r in params['manifest']['clips'] if r['kind'] == 'SOUND')
        self.assertEqual((audio['channel'], audio['pan'], audio['volume']), (64, 0, 0.7))

    def test_text_requires_font(self):
        recipe = self.recipe()
        recipe['parameters']['title'] = '没有字体'
        self.assert_rejected(recipe=recipe)

    def test_missing_and_unknown_style(self):
        template = copy.deepcopy(self.template)
        del template['style']['fit']
        self.assert_rejected(template=template)
        template = copy.deepcopy(self.template)
        template['style']['code'] = 'print(1)'
        self.assert_rejected(template=template)

    def test_unknown_input_and_source_fields(self):
        recipe = self.recipe()
        recipe['parameters']['pan'] = 0.3
        self.assert_rejected(recipe=recipe)
        recipe = self.recipe()
        recipe['context']['unsafe'] = True
        self.assert_rejected(recipe=recipe)

    def test_nonfinite_and_bool_rejected(self):
        for value in (True, float('nan'), float('inf'), 1.1):
            recipe = self.recipe()
            recipe['parameters']['width'] = value
            self.assert_rejected(recipe=recipe)

    def test_transition_rejected(self):
        template = copy.deepcopy(self.template)
        template['style']['transition_frames'] = 2
        self.assert_rejected(template=template, code='UNSUPPORTED')

    def test_transparent_color_rejected(self):
        template = copy.deepcopy(self.template)
        template['style']['background_color'][3] = 0.5
        self.assert_rejected(template=template, code='UNSUPPORTED')

    def test_duplicate_shots_rejected(self):
        recipe = self.recipe()
        recipe['parameters']['shots'][1]['id'] = 'A'
        self.assert_rejected(recipe=recipe)

    def test_resource_conflict_and_hash_rejected(self):
        recipe = self.recipe()
        recipe['context']['resources'] = [self.descriptor(self.image), {'file': str(self.image), 'expected_sha256': '0' * 64}]
        self.assert_rejected(recipe=recipe, code='CONFLICT')
        recipe = self.recipe()
        recipe['context']['expected_sha256'] = '0' * 64
        self.assert_rejected(recipe=recipe, code='CONFLICT')

    def test_relative_path_rejected(self):
        recipe = self.recipe()
        recipe['context']['file'] = 'source.blend'
        self.assert_rejected(recipe=recipe)

    def test_output_full_budget_and_no_background_shot(self):
        recipe = self.recipe()
        recipe['parameters']['output'] = {'format': 'PNG', 'shots': True, 'max_pixel_frames': 320 * 180 * 48}
        params = lib.compile_recipe(self.template, recipe)
        self.assertEqual(params['output']['shot_ids'], ['A', 'B'])
        recipe['parameters']['output']['max_pixel_frames'] -= 1
        self.assert_rejected(recipe=recipe, code='RESOURCE_LIMIT')

    def test_hdr_and_pan_and_speed_rejected(self):
        recipe = self.recipe()
        recipe['parameters']['output'] = {'format': 'MP4', 'max_pixel_frames': 1, 'color_mode': 'HDR10_PQ'}
        self.assert_rejected(recipe=recipe)
        recipe = self.recipe()
        recipe['parameters']['audio_tracks'] = [{'id': 'a', 'offset': 0, 'duration': 8,
                                                 'source': {'files': [self.descriptor(self.sound)], 'speed': 2}}]
        self.assert_rejected(recipe=recipe)
        recipe['parameters']['audio_tracks'][0]['source']['speed'] = 1
        recipe['parameters']['audio_tracks'][0]['pan'] = 0.1
        self.assert_rejected(recipe=recipe)

    def test_subtitle_overlap_allocation_and_overflow(self):
        recipe = self.recipe()
        recipe['parameters']['font'] = self.descriptor(self.font)
        recipe['parameters']['subtitles'] = [{'id': 's' + str(i), 'offset': 0, 'duration': 4, 'text': '文'} for i in range(20)]
        params = lib.compile_recipe(self.template, recipe)
        self.assertEqual({c['channel'] for c in params['manifest']['clips'] if c['kind'] == 'TEXT'}, set(range(30, 50)))
        recipe['parameters']['subtitles'].append({'id': 'overflow', 'offset': 0, 'duration': 4, 'text': '文'})
        self.assert_rejected(recipe=recipe, code='RESOURCE_LIMIT')

    def test_bounds_and_title_dependency(self):
        recipe = self.recipe()
        recipe['parameters']['font'] = self.descriptor(self.font)
        recipe['parameters']['subtitles'] = [{'id': 'bad', 'offset': 23, 'duration': 2, 'text': '越界'}]
        self.assert_rejected(recipe=recipe)
        recipe = self.recipe()
        recipe['parameters']['title_duration'] = 5
        self.assert_rejected(recipe=recipe)

    def test_exclusive_compilation_and_receipt(self):
        file = self.recipe_file(self.recipe())
        destination = self.root / 'compiled'
        receipt = lib.compile_to_directory(template_id='cg-clean', input_file=file,
                                            expected_input_sha256=lib.sha256_file(file), output_dir=destination)
        self.assertEqual((receipt['status'], receipt['execution_status']), ('compiled', 'not_run'))
        self.assertEqual(lib.sha256_file(destination / 'request.json'), receipt['request_sha256'])
        request = json.loads((destination / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(sys.modules['protocol'].validate(request), request)
        self.assertEqual(request['schema_version'], '1.0')
        with self.assertRaises(lib.TemplateError) as caught:
            lib.compile_to_directory(template_id='cg-clean', input_file=file,
                                     expected_input_sha256=lib.sha256_file(file), output_dir=destination)
        self.assertEqual(caught.exception.code, 'CONFLICT')

    def test_input_sha_duplicate_json_and_no_failed_output(self):
        file = self.recipe_file(self.recipe())
        destination = self.root / 'must-not-exist'
        with self.assertRaises(lib.TemplateError):
            lib.compile_to_directory(template_id='cg-clean', input_file=file, expected_input_sha256='0' * 64, output_dir=destination)
        self.assertFalse(destination.exists())
        file = self.root / 'duplicate-keys.json'
        file.write_bytes(b'{"a":1,"a":2}')
        with self.assertRaises(lib.TemplateError):
            lib.read_bound_json(file, lib.sha256_file(file))

    def test_custom_save_explicit_compile(self):
        custom = copy.deepcopy(self.template)
        custom['id'] = 'custom_case'
        custom['name'] = '自定义'
        file = self.recipe_file(custom)
        destination = self.root / 'custom'
        destination.mkdir()
        saved = lib.save_template(template_file=file, expected_template_sha256=lib.sha256_file(file), library_dir=destination)
        template, _ = lib.load_template(template_file=saved['saved']['file'], expected_template_sha256=saved['saved']['expected_sha256'])
        self.assertEqual(template, custom)
        lib.compile_recipe(template, self.recipe())
        with self.assertRaises(lib.TemplateError):
            lib.save_template(template_file=file, expected_template_sha256=lib.sha256_file(file), library_dir=destination)

    def test_batch_compiles_preflight_all(self):
        file = self.recipe_file(self.recipe())
        batch = {'schema_version': 'video-template-batch/1.0', 'members': [
            {'id': 'one', 'template': {'id': 'cg-clean'}, 'input': self.descriptor(file)},
            {'id': 'two', 'template': {'id': 'ai-final'}, 'input': self.descriptor(file)}]}
        batch_file = self.recipe_file(batch)
        destination = self.root / 'batch'
        result = lib.compile_batch(batch_file=batch_file, expected_batch_sha256=lib.sha256_file(batch_file), output_dir=destination)
        self.assertEqual(len(result['members']), 2)
        self.assertEqual(result['execution_status'], 'not_run')
        for member in result['members']:
            request = json.loads((destination / member['id'] / 'request.json').read_text(encoding='utf-8'))
            self.assertEqual(sys.modules['protocol'].validate(request), request)
        batch['members'][1]['input']['expected_sha256'] = '0' * 64
        bad_file = self.recipe_file(batch)
        bad_dir = self.root / 'bad-batch'
        with self.assertRaises(lib.TemplateError):
            lib.compile_batch(batch_file=bad_file, expected_batch_sha256=lib.sha256_file(bad_file), output_dir=bad_dir)
        self.assertFalse(bad_dir.exists())

    def test_batch_over_16_rejected(self):
        file = self.recipe_file(self.recipe())
        batch = {'schema_version': 'video-template-batch/1.0', 'members': [
            {'id': 'm' + str(i), 'template': {'id': 'cg-clean'}, 'input': self.descriptor(file)} for i in range(17)]}
        batch_file = self.recipe_file(batch)
        with self.assertRaises(lib.TemplateError):
            lib.compile_batch(batch_file=batch_file, expected_batch_sha256=lib.sha256_file(batch_file), output_dir=self.root / 'large-batch')

    def test_audio_channels_reuse_and_capacity(self):
        recipe = self.recipe()
        recipe['parameters']['shots'][0]['duration'] = 64
        recipe['parameters']['audio_tracks'] = [{'id': 'a' + str(i), 'offset': i, 'duration': 1,
                                                 'source': {'files': [self.descriptor(self.sound)]}} for i in range(64)]
        params = lib.compile_recipe(self.template, recipe)
        self.assertEqual({c['channel'] for c in params['manifest']['clips'] if c['kind'] == 'SOUND'}, {64})
        for row in recipe['parameters']['audio_tracks'][:33]:
            row['offset'] = 0
            row['duration'] = 2
        self.assert_rejected(recipe=recipe, code='RESOURCE_LIMIT')

    def test_native_image_directory_restriction_all_alternatives(self):
        second = self.root / 'other'
        second.mkdir()
        image = second / 'image.png'
        image.write_bytes(b'tiny cross-directory fixture')
        recipe = self.recipe()
        first = self.descriptor(self.image)
        recipe['parameters']['shots'][0] = {'id': 'A', 'kind': 'IMAGE', 'duration': 2,
                                          'source': {'files': [first]},
                                          'alternatives': [{'candidate_id': 'cross', 'source': {'files': [first, self.descriptor(image)]}}]}
        self.assert_rejected(recipe=recipe, code='UNSUPPORTED')

    def test_common_channel_overlap_check_all_types(self):
        lib.validate_channel_intervals([{'id': 'a', 'kind': 'COLOR', 'channel': 2, 'start': 1, 'duration': 3},
                                        {'id': 'b', 'kind': 'SOUND', 'channel': 2, 'start': 4, 'duration': 3}])
        with self.assertRaises(lib.TemplateError) as caught:
            lib.validate_channel_intervals([{'id': 'a', 'kind': 'COLOR', 'channel': 2, 'start': 1, 'duration': 3},
                                            {'id': 'b', 'kind': 'TEXT', 'channel': 2, 'start': 3, 'duration': 3}])
        self.assertEqual(caught.exception.code, 'CONFLICT')

    def test_mp4_requires_tools(self):
        recipe = self.recipe()
        recipe['parameters']['output'] = {'format': 'MP4', 'max_pixel_frames': 320 * 180 * 24}
        self.assert_rejected(recipe=recipe)

    def test_batch_casefold_collision_preflight(self):
        file = self.recipe_file(self.recipe())
        batch = {'schema_version': 'video-template-batch/1.0', 'members': [
            {'id': id, 'template': {'id': 'cg-clean'}, 'input': self.descriptor(file)} for id in ('A', 'a')]}
        batch_file = self.recipe_file(batch)
        destination = self.root / 'casefold-collision'
        with self.assertRaises(lib.TemplateError) as caught:
            lib.compile_batch(batch_file=batch_file, expected_batch_sha256=lib.sha256_file(batch_file), output_dir=destination)
        self.assertEqual(caught.exception.code, 'CONFLICT')
        self.assertFalse(destination.exists())

    def test_windows_reserved_save_names_preflight(self):
        custom_dir = self.root / 'reserved-save'
        custom_dir.mkdir()
        for identity in ('CON', 'prn', 'AUX', 'nul', 'COM1', 'com9', 'LPT1', 'lpt9'):
            template = copy.deepcopy(self.template)
            template['id'] = identity
            file = self.recipe_file(template)
            with self.assertRaises(lib.TemplateError):
                lib.save_template(template_file=file, expected_template_sha256=lib.sha256_file(file), library_dir=custom_dir)
        self.assertEqual(list(custom_dir.iterdir()), [])
        # The general runtime shot ID vocabulary is preserved.
        recipe = self.recipe()
        recipe['parameters']['shots'][0]['id'] = 'CON'
        self.assertEqual(lib.compile_recipe(self.template, recipe)['manifest']['clips'][1]['shot_id'], 'CON')

    def test_windows_reserved_batch_names_preflight(self):
        file = self.recipe_file(self.recipe())
        for identity in ('CON', 'prn', 'AUX', 'nul', 'COM1', 'com9', 'LPT1', 'lpt9'):
            batch = {'schema_version': 'video-template-batch/1.0', 'members': [
                {'id': 'valid', 'template': {'id': 'cg-clean'}, 'input': self.descriptor(file)},
                {'id': identity, 'template': {'id': 'cg-clean'}, 'input': self.descriptor(file)}]}
            batch_file = self.recipe_file(batch)
            output = self.root / ('reserved-batch-' + identity)
            with self.assertRaises(lib.TemplateError):
                lib.compile_batch(batch_file=batch_file, expected_batch_sha256=lib.sha256_file(batch_file), output_dir=output)
            self.assertFalse(output.exists())

    def test_isolated_surrogate_preflight_and_serialization(self):
        template = copy.deepcopy(self.template)
        template['description'] = '\ud800'
        with self.assertRaises(lib.TemplateError):
            lib.validate_template(template)
        recipe = self.recipe()
        recipe['parameters']['scene'] = '\udfff'
        self.assert_rejected(recipe=recipe)
        file = self.root / 'surrogate-definition.json'
        file.write_text(json.dumps(template, ensure_ascii=True), encoding='utf-8')
        target = self.root / 'surrogate-custom'
        target.mkdir()
        with self.assertRaises(lib.TemplateError):
            lib.save_template(template_file=file, expected_template_sha256=lib.sha256_file(file), library_dir=target)
        self.assertEqual(list(target.iterdir()), [])
        json_path = self.root / 'must-not-create-empty.json'
        with self.assertRaises(UnicodeEncodeError):
            lib._write_json(json_path, {'invalid': '\ud800'})
        self.assertFalse(json_path.exists())

    def test_failed_marker_cannot_mask_original_exception(self):
        file = self.recipe_file(self.recipe())
        output = self.root / 'fault-injected-package'
        original = lib.TemplateError('ORIGINAL_FAILURE', 'original failure')
        with patch.object(lib, '_package', side_effect=original), patch.object(lib, '_write_json', side_effect=OSError('marker unavailable')):
            with self.assertRaises(lib.TemplateError) as caught:
                lib.compile_to_directory(template_id='cg-clean', input_file=file,
                                         expected_input_sha256=lib.sha256_file(file), output_dir=output)
        self.assertIs(caught.exception, original)
        self.assertFalse((output / 'build-receipt.json').exists())

    def test_movie_schema_requires_single_source_and_alternative(self):
        recipe = self.recipe()
        first = self.descriptor(self.image)
        second = self.descriptor(self.sound)
        recipe['parameters']['shots'][0] = {'id': 'A', 'kind': 'MOVIE', 'duration': 12, 'source': {'files': [first, second]}}
        with self.assertRaises(lib.TemplateError):
            lib.validate(recipe, lib.schemas()['input'])
        recipe['parameters']['shots'][0]['source']['files'] = [first]
        recipe['parameters']['shots'][0]['alternatives'] = [{'candidate_id': 'multiple', 'source': {'files': [first, second]}}]
        with self.assertRaises(lib.TemplateError):
            lib.validate(recipe, lib.schemas()['input'])

    def test_sound_schema_unity_only(self):
        recipe = self.recipe()
        recipe['parameters']['audio_tracks'] = [{'id': 'a', 'offset': 0, 'duration': 8,
                                                 'source': {'files': [self.descriptor(self.sound)], 'speed': 1.0, 'freeze': False}}]
        lib.validate(recipe, lib.schemas()['input'])
        lib.compile_recipe(self.template, recipe)
        for value in (2, True, 0.5):
            recipe['parameters']['audio_tracks'][0]['source']['speed'] = value
            with self.assertRaises(lib.TemplateError):
                lib.validate(recipe, lib.schemas()['input'])
        recipe['parameters']['audio_tracks'][0]['source']['speed'] = 1
        recipe['parameters']['audio_tracks'][0]['source']['freeze'] = True
        with self.assertRaises(lib.TemplateError):
            lib.validate(recipe, lib.schemas()['input'])

    def test_output_schema_exposes_sdr_only(self):
        schema = lib.schemas()['input']['properties']['parameters']['properties']['output']
        self.assertEqual(schema['properties']['color_mode']['enum'], ['SDR'])
        self.assertNotIn('hdr', schema['properties'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
