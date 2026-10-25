# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed signal contracts and opt-in real Blender mechanism qualification.

Host: python THIS --host --output ABSENT_ABSOLUTE_DIR
Blender: --background --factory-startup --python-exit-code 1 --python THIS --
         --output ABSENT_ABSOLUTE_DIR [--baseline-core ORIGINAL_CORE_PY]
The native probe uses emission to isolate numerical fields from artistic shading.
"""
import argparse
from copy import deepcopy
import importlib.util
import json
import struct
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from material_workflow_addon import procedural_contract as pc


def signal(identity, kind, parameters=None, inputs=None):
    return {'id': identity, 'type': kind, 'parameters': parameters or {}, 'inputs': inputs or {}}


def f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def manifest(signals, output='field', identity='signal_test'):
    return {'schema_version': '2.0', 'context': {'scene': 'Scene', 'view_layer': 'ViewLayer'},
            'target': {'object': 'Cube', 'uv_layer': 'UVMap'},
            'material': {'id': identity, 'name': identity}, 'signals': signals,
            'layers': [{'id': 'base', 'name': 'Base', 'channels': {'roughness': output}}],
            'preview': {'device': {'backend': 'CPU'}, 'views': [{'id': 'front'}]}}


class Contracts(unittest.TestCase):
    def test_defaults_and_output_types(self):
        spec = manifest([signal('coords', 'coordinate'), signal('warp', 'warp', inputs={'vector': 'coords'}),
                         signal('field', 'noise', inputs={'vector': 'warp'}),
                         signal('pores', 'voronoi'), signal('wood', 'wood'),
                         signal('mapped', 'remap', inputs={'value': 'field'})])
        out = pc.normalize_manifest(spec)
        types = pc.signal_types(out)
        self.assertEqual((types['coords'], types['warp'], types['pores']), ('VECTOR', 'VECTOR', 'VALUE'))
        by_id = {s['id']: s for s in out['signals']}
        self.assertEqual(by_id['warp']['parameters']['strength'], [f32(.1)] * 3)
        self.assertEqual(by_id['field']['parameters']['lacunarity'], 2)
        self.assertEqual(by_id['wood']['parameters']['detail_scale'], 1)
        self.assertEqual(by_id['wood']['parameters']['profile'], 'SIN')
        self.assertEqual(by_id['pores']['parameters']['feature'], 'F1')

    def test_vector_rejected_from_scalar_inputs_and_layers(self):
        initial = [signal('coords', 'coordinate'), signal('field', 'constant', {'value': .5})]
        for kind, inputs, parameters in [
            ('math', {'a': 'coords', 'b': 'field'}, {'operation': 'ADD'}),
            ('remap', {'value': 'coords'}, {}),
            ('mix', {'a': 'coords', 'b': 'coords', 'factor': 'field'}, {}),
            ('mix', {'a': 'field', 'b': 'field', 'factor': 'coords'}, {})]:
            with self.subTest(kind=kind, inputs=inputs), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest(initial + [signal('bad', kind, parameters, inputs)]))
        for channel in ('height', 'base_color'):
            bad = manifest(initial); bad['layers'][0]['channels'] = {channel: 'coords'}
            with self.subTest(channel=channel), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(bad)
        bad = manifest(initial); bad['layers'].append({'id': 'over', 'name': 'Over', 'mask': 'coords'})
        with self.assertRaises(pc.WorkflowError):
            pc.normalize_manifest(bad)

    def test_vector_inputs_require_vector_and_warp_requires_input(self):
        for kind in ('noise', 'wood', 'height_band', 'voronoi', 'warp'):
            with self.subTest(kind=kind), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('value', 'constant', {'value': .5}),
                    signal('field', kind, inputs={'vector': 'value'})]))
        with self.assertRaises(pc.WorkflowError):
            pc.normalize_manifest(manifest([signal('field', 'warp')]))

    def test_cycle_and_unknown_source_rejected(self):
        with self.assertRaisesRegex(pc.WorkflowError, 'cycle'):
            pc.normalize_manifest(manifest([signal('a', 'warp', inputs={'vector': 'b'}),
                signal('b', 'warp', inputs={'vector': 'a'}), signal('field', 'noise', inputs={'vector': 'a'})]))
        with self.assertRaises(pc.WorkflowError):
            pc.normalize_manifest(manifest([signal('field', 'arbitrary_python')]))

    def test_coordinate_vectors_finite_and_positive_sampling(self):
        for key, value in [('sample_scale', [0, 1, 1]), ('sample_scale', [-1, 1, 1]),
                           ('strength', [float('nan'), 0, 0]), ('strength', [float('inf'), 0, 0]),
                           ('strength', [True, 0, 0]), ('strength', [1, 2])]:
            with self.subTest(key=key, value=value), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('c', 'coordinate'),
                    signal('w', 'warp', {key: value}, {'vector': 'c'}),
                    signal('field', 'noise', inputs={'vector': 'w'})]))
        pc.normalize_manifest(manifest([signal('c', 'coordinate'),
            signal('w', 'warp', {'strength': [-.1, 0, .2]}, {'vector': 'c'}),
            signal('field', 'noise', inputs={'vector': 'w'})]))

    def test_remap_reversed_signed_and_equal_limits(self):
        for interpolation in ('LINEAR', 'SMOOTHSTEP', 'SMOOTHERSTEP'):
            out = pc.normalize_manifest(manifest([signal('v', 'constant', {'value': .75}),
                signal('field', 'remap', {'from_min': 1, 'from_max': 0, 'to_min': -2, 'to_max': 2,
                                        'interpolation': interpolation, 'clamp': False}, {'value': 'v'})]))
            self.assertEqual(out['signals'][1]['parameters']['to_min'], -2)
        for limits in ({'from_min': 1, 'from_max': 1}, {'to_min': float('nan')}):
            with self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('v', 'constant', {'value': .5}),
                    signal('field', 'remap', limits, {'value': 'v'})]))

    def test_unsupported_variants_rejected(self):
        for kind, parameters in [('voronoi', {'feature': 'F2'}), ('voronoi', {'seed': 3}),
                                 ('voronoi', {'randomness': 2}), ('wood', {'profile': 'SQUARE'}),
                                 ('remap', {'interpolation': 'STEPPED'})]:
            inputs = {'value': 'v'} if kind == 'remap' else {}
            with self.subTest(kind=kind, parameters=parameters), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('v', 'constant', {'value': .5}),
                    signal('field', kind, parameters, inputs)]))

    def test_float32_boundary_precision_without_general_epsilon(self):
        rounded_minimum = f32(.0001)
        bits = struct.unpack('I', struct.pack('f', rounded_minimum))[0]
        adjacent_below = struct.unpack('f', struct.pack('I', bits-1))[0]
        for kind in ('noise', 'wood', 'voronoi'):
            for value in (.0001, rounded_minimum):
                with self.subTest(kind=kind, value=value):
                    out = pc.normalize_manifest(manifest([signal('field', kind, {'scale': value})]))
                    self.assertEqual(out['signals'][0]['parameters']['scale'], .0001)
                    self.assertEqual(pc.normalize_manifest(out), out)
            for value in (adjacent_below, .0001-1e-13, .00009):
                with self.subTest(kind=kind, invalid=value), self.assertRaises(pc.WorkflowError):
                    pc.normalize_manifest(manifest([signal('field', kind, {'scale': value})]))

    def test_float32_remap_distinctness_forward_and_reverse(self):
        adjacent = f32(1 + 2**-23)
        for limits in ((1, 1.00000001), (1.00000001, 1), (-1, -1.00000001)):
            with self.subTest(collapse=limits), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('v', 'constant', {'value': .5}),
                    signal('field', 'remap', {'from_min': limits[0], 'from_max': limits[1]}, {'value': 'v'})]))
        for limits in ((1, adjacent), (adjacent, 1)):
            out = pc.normalize_manifest(manifest([signal('v', 'constant', {'value': .5}),
                signal('field', 'remap', {'from_min': limits[0], 'from_max': limits[1]}, {'value': 'v'})]))
            self.assertNotEqual(out['signals'][1]['parameters']['from_min'], out['signals'][1]['parameters']['from_max'])

    def test_float32_overflow_underflow_and_comparison_classified(self):
        from material_workflow_addon import procedural_core as core
        for value in (1e40, -1e40, 1e-60, float('nan'), float('inf')):
            with self.subTest(strength=value), self.assertRaises(pc.WorkflowError):
                pc.normalize_manifest(manifest([signal('c', 'coordinate'),
                    signal('w', 'warp', {'strength': [value, 0, 0]}, {'vector': 'c'}),
                    signal('field', 'noise', inputs={'vector': 'w'})]))
            with self.subTest(compare=value), self.assertRaises(pc.WorkflowError):
                core._same_control(value, 0)


def native(args):
    import array
    import bpy
    import math
    from mathutils import Vector
    from material_workflow_addon import procedural_core as core, engine_compat

    output = Path(args.output)
    if not output.is_absolute() or output.exists():
        raise ValueError('Native output must be an absent absolute directory')
    output.mkdir(parents=True)
    checks, images = [], []

    def check(name, condition, **evidence):
        checks.append({'name': name, 'pass': bool(condition), **evidence})
        (output / 'checks.json').write_text(json.dumps({'checks': checks, 'images': images}, indent=2), encoding='utf-8')
        if not condition:
            raise AssertionError(name)
        print('PASS', name, flush=True)

    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'; scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.resolution_x = scene.render.resolution_y = 128; scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'; scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '8'
    scene.view_settings.view_transform = 'Standard'; scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0; scene.view_settings.gamma = 1
    obj = bpy.data.objects['Cube']
    # Front face has object XY variation; camera and texture stay fixed for every probe.
    camera = scene.camera; camera.location = (0, -5, 0)
    camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = 'ORTHO'; camera.data.ortho_scale = 2

    def build(signals, label, field='field', backend=core):
        spec = manifest(signals, field, label)
        backend.apply_material(spec, work_dir=str(output / label))
        mat = obj.material_slots[0].material
        check(label + '_managed', core.verify_managed_state(mat)['ok'])
        return mat

    def render(mat, label):
        probe = bpy.data.materials.new('Probe/' + label); probe.use_nodes = True
        nodes = probe.node_tree.nodes; nodes.clear()
        group = nodes.new('ShaderNodeGroup'); group.node_tree = mat.node_tree.nodes['MWP Controls'].node_tree
        emission = nodes.new('ShaderNodeEmission'); end = nodes.new('ShaderNodeOutputMaterial')
        probe.node_tree.links.new(group.outputs['roughness'], emission.inputs['Color'])
        probe.node_tree.links.new(emission.outputs[0], end.inputs['Surface'])
        obj.material_slots[0].material = probe
        scene.render.filepath = str(output / (label + '.png'))
        try:
            bpy.ops.render.render(write_still=True)
            image = bpy.data.images.load(scene.render.filepath, check_existing=False)
            pixels = array.array('f', [0]) * len(image.pixels); image.pixels.foreach_get(pixels)
            images.append({'file': label + '.png', 'bytes': Path(scene.render.filepath).stat().st_size,
                           'size': list(image.size)})
            check(label + '_bounded_decode', max(image.size) <= 768 and Path(scene.render.filepath).stat().st_size <= 2*1024*1024)
            bpy.data.images.remove(image)
            return pixels
        finally:
            obj.material_slots[0].material = mat
            bpy.data.materials.remove(probe)

    def difference(a, b):
        return sum(abs(x-y) for i, (x, y) in enumerate(zip(a, b)) if i % 4 != 3) / (len(a)*.75)

    # Reproduce the independently reported float32 failures using actual RNA
    # controls, compatibility scans and same-ID replay, before the image probes.
    for kind in ('noise', 'wood', 'voronoi'):
        signals = [signal('field', kind, {'scale': .0001})]
        label = 'float32_min_' + kind
        mat = build(signals, label)
        controls = mat.node_tree.nodes['MWP Controls']
        check(label + '_rna_boundary', controls.inputs['S/field/scale'].default_value == f32(.0001))
        check(label + '_readback', core.get_stored_manifest(mat)['signals'][0]['parameters']['scale'] == .0001)
        for engine in ('CYCLES', 'BLENDER_EEVEE'):
            check(label + '_compat_' + engine, bool(engine_compat.validate_material(mat, engine)))
        core.apply_material(manifest(signals, identity=label))
        mat = obj.material_slots[0].material
        check(label + '_replay', core.get_stored_manifest(mat)['signals'][0]['parameters']['scale'] == .0001)
        controls = mat.node_tree.nodes['MWP Controls']
        controls.inputs['S/field/scale'].default_value = .00009
        try:
            core.get_stored_manifest(mat)
        except pc.WorkflowError:
            check(label + '_true_manual_out_of_range_rejected', True)
        else:
            check(label + '_true_manual_out_of_range_rejected', False)
        controls.inputs['S/field/scale'].default_value = .0001
        check(label + '_boundary_restored', core.get_stored_manifest(mat)['signals'][0]['parameters']['scale'] == .0001)

    def rejected_without_mutation(signals, label):
        bound = obj.material_slots[0].material
        before = core._snapshot(bound)
        material_names = set(bpy.data.materials.keys()); group_names = set(bpy.data.node_groups.keys())
        try:
            core.apply_material(manifest(signals, identity=label))
        except pc.WorkflowError as error:
            check(label + '_rejected_before_binding', error.code == 'INVALID_REQUEST'
                and obj.material_slots[0].material == bound and core._snapshot(bound) == before
                and set(bpy.data.materials.keys()) == material_names and set(bpy.data.node_groups.keys()) == group_names,
                code=error.code, message=str(error))
        else:
            check(label + '_rejected_before_binding', False)

    for label, limits in [('forward', (1, 1.00000001)), ('reverse', (1.00000001, 1))]:
        rejected_without_mutation([signal('v', 'constant', {'value': .5}),
            signal('field', 'remap', {'from_min': limits[0], 'from_max': limits[1]}, {'value': 'v'})], 'float32_collapse_' + label)
    for label, limits in [('forward', (1, f32(1+2**-23))), ('reverse', (f32(1+2**-23), 1))]:
        mat = build([signal('v', 'constant', {'value': .5}),
            signal('field', 'remap', {'from_min': limits[0], 'from_max': limits[1]}, {'value': 'v'})], 'float32_adjacent_' + label)
        params = core.get_stored_manifest(mat)['signals'][1]['parameters']
        check('float32_adjacent_' + label + '_distinct_after_rna', params['from_min'] != params['from_max'])
    rejected_without_mutation([signal('c', 'coordinate'),
        signal('w', 'warp', {'strength': [1e40, 0, 0]}, {'vector': 'c'}),
        signal('field', 'noise', inputs={'vector': 'w'})], 'float32_strength_overflow')
    try:
        core._same_control(1e40, f32(1))
    except pc.WorkflowError:
        check('float32_comparison_overflow_classified', True)
    else:
        check('float32_comparison_overflow_classified', False)

    # The original unpublished 2.0 graph ignored scalar Scale because its
    # coordinate Scale collided. Preserve the evidence, not that incorrect image.
    if args.baseline_core:
        oldspec = importlib.util.spec_from_file_location('material_workflow_addon._baseline_signal_core', args.baseline_core)
        old = importlib.util.module_from_spec(oldspec); oldspec.loader.exec_module(old)
        for kind in ('noise', 'wood'):
            signals = [signal('field', kind, {'scale': 4, 'seed': 3})]
            oldmat = build(signals, 'legacy_' + kind, backend=old)
            before = render(oldmat, 'legacy_' + kind)
            try:
                core.get_stored_manifest(oldmat)
            except pc.WorkflowError as error:
                check('legacy_' + kind + '_requires_new_identity', error.code == 'managed_conflict')
            else:
                check('legacy_' + kind + '_requires_new_identity', False)
            before_state = core._snapshot(oldmat)
            try:
                core.apply_material(manifest(signals, identity='legacy_' + kind))
            except pc.WorkflowError as error:
                check('legacy_' + kind + '_update_rejected_without_mutation', error.code == 'managed_conflict' and core._snapshot(oldmat) == before_state)
            else:
                check('legacy_' + kind + '_update_rejected_without_mutation', False)
            after = render(build(signals, 'current_' + kind), 'current_' + kind)
            delta = difference(before, after)
            check('legacy_' + kind + '_scale_collision_corrected', delta > .01, mean_absolute_difference=delta,
                  old_candidate_visual_evidence_invalidated=True)

    direct = render(build([signal('field', 'noise', {'scale': 4, 'seed': 2})], 'implicit'), 'implicit')
    explicit_signals = [signal('coords', 'coordinate'), signal('field', 'noise', {'scale': 4, 'seed': 2}, {'vector': 'coords'})]
    explicit = render(build(explicit_signals, 'explicit'), 'explicit')
    check('explicit_coordinate_matches_legacy_mapping', difference(direct, explicit) == 0)
    transformed = {'scale': [2, 1, 1], 'rotation': [0, 0, .2], 'translation': [.1, 0, 0]}
    implicit_mapped = render(build([signal('field', 'noise', {'scale': 4, 'seed': 2, 'coordinate': transformed})], 'implicit_mapped'), 'implicit_mapped')
    mapped_material = build([signal('coords', 'coordinate'),
        signal('field', 'noise', {'scale': 4, 'seed': 2, 'coordinate': transformed}, {'vector': 'coords'})], 'explicit_mapped')
    explicit_mapped = render(mapped_material, 'explicit_mapped')
    check('vector_source_then_per_signal_mapping', difference(implicit_mapped, explicit_mapped) == 0)
    mapped_tree = mapped_material.node_tree.nodes['MWP Controls'].node_tree
    mapping = mapped_tree.nodes['MWP/field/mapping']
    connection = mapping.inputs['Vector'].links[0]
    original_output = connection.from_socket
    mapped_tree.links.remove(connection)
    check('protect_vector_connection_edit', not core.verify_managed_state(mapped_material)['ok'])
    mapped_tree.links.new(original_output, mapping.inputs['Vector'])
    check('restore_vector_connection', core.verify_managed_state(mapped_material)['ok'])
    scaled_images = []
    for scale in (2, 6):
        mat = build([signal('field', 'noise', {'scale': scale, 'seed': 2})], 'scale_' + str(scale))
        group = mat.node_tree.nodes['MWP Controls'].node_tree
        node = group.nodes['MWP/field/noise']
        check('scale_' + str(scale) + '_receives_scalar', len(node.inputs['Scale'].links) == 1 and node.inputs['Scale'].links[0].from_socket.type == 'VALUE')
        pixels = render(mat, 'scale_' + str(scale)); scaled_images.append(pixels)
        gradient = sum(abs(pixels[(y*128+x+1)*4]-pixels[(y*128+x)*4]) for y in range(16,112) for x in range(16,111))/(96*95)
        if scale == 2:
            low_gradient = gradient
        else:
            check('scale_changes_pattern_density', gradient > low_gradient, scale2_gradient=low_gradient, scale6_gradient=gradient)
    check('scale_changes_image', difference(*scaled_images) > .01)
    control = mat.node_tree.nodes['MWP Controls']
    control.inputs['S/field/coordinate/scale'].default_value = [1, .3, 1]
    control.inputs['S/field/scale'].default_value = 7
    readback = core.get_stored_manifest(mat)['signals'][0]['parameters']
    check('coordinate_and_scalar_scale_readback_independent', readback['scale'] == 7 and abs(readback['coordinate']['scale'][1]-.3)<1e-6)
    chain = [signal('coords', 'coordinate'), signal('warp', 'warp', {'strength': [0, 0, 0]}, {'vector': 'coords'}),
             signal('field', 'noise', {'scale': 4, 'seed': 2}, {'vector': 'warp'})]
    zero = render(build(chain, 'zero_warp'), 'zero_warp')
    check('warp_zero_exact_image_equivalence', difference(explicit, zero) == 0)
    chain[1]['parameters']['strength'] = [.35, .2, .15]
    mat = build(chain, 'nonzero_warp')
    warped = render(mat, 'nonzero_warp')
    check('warp_nonzero_image_changes', difference(zero, warped) > .01, mean_absolute_difference=difference(zero, warped))
    tree = mat.node_tree.nodes['MWP Controls'].node_tree
    noise = tree.nodes['MWP/warp/noise']
    check('warp_uses_noise_color_vector', any(l.from_socket.name == 'Color' and l.to_node.name == 'MWP/warp/centered' for l in tree.links))
    check('warp_explicit_fbm_normalized', noise.noise_type == 'FBM' and noise.normalize and noise.noise_dimensions == '4D')
    control = mat.node_tree.nodes['MWP Controls']
    control.inputs['S/warp/strength'].default_value = [-.1, .1, .2]
    live = core.get_stored_manifest(mat)
    check('warp_exposed_vector_roundtrip', abs(live['signals'][1]['parameters']['strength'][0] + .1) < 1e-6)
    for prop, alternate in [('noise_type', 'MULTIFRACTAL'), ('normalize', False), ('noise_dimensions', '3D')]:
        original = getattr(noise, prop); setattr(noise, prop, alternate)
        check('protect_noise_' + prop, not core.verify_managed_state(mat)['ok'])
        setattr(noise, prop, original)
        check('restore_noise_' + prop, core.verify_managed_state(mat)['ok'])

    for feature in ('F1', 'DISTANCE_TO_EDGE'):
        mat = build([signal('coords', 'coordinate'), signal('warp', 'warp', inputs={'vector': 'coords'}),
                     signal('field', 'voronoi', {'feature': feature, 'scale': 6}, {'vector': 'warp'})], 'voronoi_' + feature)
        pixels = render(mat, 'voronoi_' + feature)
        values = pixels[0::4]
        check('voronoi_' + feature + '_nonconstant', max(values)-min(values) > .05)
        for engine in ('CYCLES', 'BLENDER_EEVEE'):
            check('compat_' + feature + '_' + engine, 'ShaderNodeTexVoronoi' in engine_compat.validate_material(mat, engine)['node_types'])
        scene.render.engine = 'BLENDER_EEVEE'
        render(mat, 'eevee_voronoi_' + feature)
        scene.render.engine = 'CYCLES'
        if feature == 'F1':
            v = mat.node_tree.nodes['MWP Controls'].node_tree.nodes['MWP/field/voronoi']
            for prop, alternate in [('feature', 'DISTANCE_TO_EDGE'), ('distance', 'MANHATTAN'), ('normalize', True)]:
                original = getattr(v, prop); setattr(v, prop, alternate)
                check('protect_voronoi_' + prop, not core.verify_managed_state(mat)['ok'])
                setattr(v, prop, original)

    # Signed value (-1), reversed source range, then encode to +.25 for an emission probe.
    remaps = [signal('v', 'constant', {'value': .75}),
              signal('signed', 'remap', {'from_min': 1, 'from_max': 0, 'to_min': -2, 'to_max': 2}, {'value': 'v'}),
              signal('field', 'remap', {'from_min': -2, 'from_max': 2}, {'value': 'signed'})]
    mat = build(remaps, 'signed_reverse')
    pixels = render(mat, 'signed_reverse')
    center = pixels[(64*128+64)*4]
    # This saved Standard PNG probe is display encoded; use the known sRGB
    # transfer before comparing a mathematical scalar to the disk pixel.
    linear_center = center/12.92 if center <= .04045 else ((center+.055)/1.055)**2.4
    check('remap_reverse_and_signed_numeric', abs(linear_center-.25) < .01,
          stored_srgb_center=center, decoded_linear_center=linear_center, expected=.25)
    for interpolation in ('SMOOTHSTEP', 'SMOOTHERSTEP'):
        remaps[1]['parameters']['interpolation'] = interpolation
        m = build(remaps, 'remap_' + interpolation)
        render(m, 'remap_' + interpolation)
    protected_remap = m.node_tree.nodes['MWP Controls'].node_tree.nodes['MWP/signed/remap']
    protected_remap.interpolation_type = 'LINEAR'
    check('protect_remap_interpolation', not core.verify_managed_state(m)['ok'])
    protected_remap.interpolation_type = 'SMOOTHERSTEP'
    check('restore_remap_interpolation', core.verify_managed_state(m)['ok'])
    # Structural profile edits are protected separately from detail_scale controls.
    mat = build([signal('field', 'wood', {'profile': 'TRI', 'detail_scale': 2})], 'wave_profile')
    node = mat.node_tree.nodes['MWP Controls'].node_tree.nodes['MWP/field/wood']
    node.wave_profile = 'SAW'; check('protect_wave_profile', not core.verify_managed_state(mat)['ok']); node.wave_profile = 'TRI'
    check('restore_wave_profile', core.verify_managed_state(mat)['ok'])
    # Build a final graph containing all new signal classes, then independently reopen it.
    final_signals = [signal('coords', 'coordinate'), signal('warp', 'warp', {'strength': [.12, .08, .05]}, {'vector': 'coords'}),
                     signal('distance', 'voronoi', {'feature': 'F1'}, {'vector': 'warp'}),
                     signal('field', 'remap', {'to_min': -.2, 'to_max': .8}, {'value': 'distance'})]
    mat = build(final_signals, 'saved_signals')
    final_image = render(mat, 'saved_signals')
    check('finite_rendered_values', all(math.isfinite(x) for x in final_image))
    bpy.ops.wm.save_as_mainfile(filepath=str(output / 'signals.blend'))
    (output / 'expected-manifest.json').write_text(json.dumps(core.get_stored_manifest(mat), indent=2), encoding='utf-8')
    check('saved_graph', (output / 'signals.blend').is_file())
    summary = {'status': 'pass', 'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
               'checks': checks, 'images': images, 'scope': 'Mechanisms only; no artistic acceptance, bake or external-engine equivalence'}
    (output / 'report.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('SIGNAL_REPORT', output / 'report.json', flush=True)


def reopen(args):
    import bpy
    from material_workflow_addon import procedural_core as core
    output = Path(args.native_output)
    mat = bpy.data.objects['Cube'].material_slots[0].material
    expected = json.loads((output / 'expected-manifest.json').read_text(encoding='utf-8'))
    result = {'managed_state': core.verify_managed_state(mat)['ok'],
              'manifest_matches': core.get_stored_manifest(mat) == pc.normalize_manifest(expected),
              'opened_file': bpy.data.filepath}
    assert result['managed_state'] and result['manifest_matches']
    (Path(args.output) / 'reopen.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('SIGNAL_REOPEN', json.dumps(result), flush=True)


if __name__ == '__main__':
    raw = sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--baseline-core')
    parser.add_argument('--reopen', action='store_true')
    parser.add_argument('--native-output')
    parser.add_argument('--host', action='store_true')
    options = parser.parse_args(raw)
    if options.host:
        output = Path(options.output)
        if not output.is_absolute() or output.exists():
            raise ValueError('Host output must be an absent absolute directory')
        output.mkdir(parents=True)
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
        (output/'report.json').write_text(json.dumps({'status':'pass' if result.wasSuccessful() else 'fail','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)},indent=2),encoding='utf-8')
        raise SystemExit(0 if result.wasSuccessful() else 1)
    import bpy
    if not bpy.app.background or bpy.app.version < (5,2,0):
        raise RuntimeError('Native signals require background Blender 5.2 or newer')
    if options.reopen:
        output=Path(options.output)
        if not output.is_absolute() or output.exists() or not options.native_output:
            raise ValueError('Reopen needs a new absolute output and explicit --native-output')
        if not Path(options.native_output).is_absolute():
            raise ValueError('Native source-output must be absolute')
        output.mkdir(parents=True)
        reopen(options)
    else:
        native(options)
