# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable Thickness regression using a self-contained cube and 32px bakes.

Build: blender --background --factory-startup --python-exit-code 1 --python THIS --
       --output ABSOLUTE_NEW_DIRECTORY
Independent reopen: same command with --output ANOTHER_NEW_DIRECTORY
                    --verify-fixtures FIRST_DIRECTORY
No source blend, installed add-on, private fixture or network is needed.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import json
from pathlib import Path
import struct
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bpy
from material_workflow_addon import procedural_core as core, procedural_bake as bake
from material_workflow_addon import procedural_contract as contract, procedural_examples as examples
from material_workflow_addon.contract import WorkflowError


def recipe(name):
    return {'schema_version': '2.0',
            'context': {'scene': 'Scene', 'view_layer': 'ViewLayer'},
            'target': {'object': name, 'uv_layer': 'UVMap'},
            'material': {'id': name, 'name': name},
            'signals': [{'id': 'rough', 'type': 'constant', 'parameters': {'value': .1}, 'inputs': {}}],
            'layers': [{'id': 'glass', 'name': 'Glass', 'channels': {'roughness': 'rough'},
                        'values': {'transmission': 1, 'ior': 1.45}}],
            'preview': {'device': {'backend': 'CPU'}, 'views': [{'id': 'front'}]}}


def output(material):
    return next(n for n in material.node_tree.nodes
                if n.bl_idname == 'ShaderNodeOutputMaterial' and n.is_active_output)


def check(name, condition, **facts):
    report['checks'].append({'name': name, 'pass': bool(condition), **facts})
    if not condition:
        raise AssertionError(name)
    print('PASS', name, flush=True)


def reject(name, fn, code):
    try:
        fn()
    except WorkflowError as error:
        check(name, error.code == code, error_code=error.code)
    else:
        check(name, False)


def inspect_fixture(source):
    expected = json.loads((source / 'fixtures.json').read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=str(source / 'optics.blend'), load_ui=False, use_scripts=False)
    for name, row in expected.items():
        obj = bpy.data.objects[name]
        material = obj.material_slots[0].material
        check(name + '_author_reopen', core.verify_managed_state(material)['ok']
              and core.get_stored_manifest(material) == row['manifest']
              and material[core.STATE] == row['state']
              and material.thickness_mode == row['thickness_mode']
              and bake._output_thickness(output(material)) == row['thickness'])
        if 'rebuilt_object' in row:
            rebuilt = bpy.data.objects[row['rebuilt_object']].material_slots[0].material
            check(name + '_rebuilt_reopen', rebuilt.thickness_mode == row['thickness_mode']
                  and bake._output_thickness(output(rebuilt)) == row['thickness'])


def build(destination):
    bpy.ops.wm.read_factory_settings(use_empty=False)
    # Existing public recipes must remain opt-out. Normalization must not add a
    # surface key or mutate the caller; no old checkout or audit fixture needed.
    for family in examples.FAMILIES:
        value = examples.make_recipe(family, recipe('unused')['context'], recipe('unused')['target'], 'Compat' + family)
        original = copy.deepcopy(value)
        normalized = contract.normalize_manifest(value)
        check('legacy_recipe_' + family, 'surface' not in normalized
              and contract.normalize_manifest(normalized) == normalized and value == original)
    for bad in (True, -1, 1001, float('nan'), float('inf'), 1e-50):
        value = recipe('Invalid')
        value['surface'] = {'thickness_mode': 'SLAB', 'thickness': bad}
        reject('invalid_thickness_' + repr(bad), lambda value=value: contract.normalize_manifest(value), 'INVALID_REQUEST')

    expected = {}
    plans = [('Legacy', None), ('Sphere', {'thickness_mode': 'SPHERE'}),
             ('Auto', {'thickness_mode': 'SLAB'}),
             ('Zero', {'thickness_mode': 'SLAB', 'thickness': 0}),
             ('Positive', {'thickness_mode': 'SLAB', 'thickness': .1})]
    for index, (name, surface) in enumerate(plans):
        bpy.ops.mesh.primitive_cube_add(location=(index * 3, 0, 0))
        obj = bpy.context.object
        obj.name = name
        spec = recipe(name)
        if surface is not None:
            spec['surface'] = surface
        core.apply_material(spec, work_dir=str(destination / name))
        material = obj.material_slots[0].material
        explicit = surface is not None and 'thickness' in surface
        thickness = {'mode': 'EXPLICIT' if explicit else 'AUTO', 'space': 'OBJECT',
                     'value': struct.unpack('f', struct.pack('f', surface['thickness']))[0] if explicit else None}
        check(name + '_build', core.verify_managed_state(material)['ok']
              and output(material).inputs['Thickness'].is_linked == explicit
              and bake._output_thickness(output(material)) == thickness
              and material.thickness_mode == ('SPHERE' if surface is None else surface['thickness_mode']))
        if surface is None:
            # Independently form the pre-surface snapshot shape, rather than
            # deleting keys from an expected new snapshot to admit changes.
            old_state = core._json(core._tree_snapshot(material.node_tree, json.loads(material['mwp_controls'])))
            check('legacy_snapshot_identity', material[core.STATE] == old_state
                  and set(json.loads(old_state)) == {'nodes', 'interface', 'links'}
                  and 'surface' not in core.get_stored_manifest(material))
        expected[name] = {'manifest': core.get_stored_manifest(material), 'state': material[core.STATE],
                          'thickness_mode': material.thickness_mode, 'thickness': thickness}
        if name in ('Auto', 'Zero', 'Positive'):
            before = core.get_stored_manifest(material)
            baked = bake.bake_channels(obj, material, str(destination / 'baked' / name),
                {'channels': ['base_color', 'normal'], 'size': 32, 'samples': 1, 'margin': 1, 'bit_depth': '16'})
            rebuilt = bake.rebuild_material(baked)
            check(name + '_bake_rebuild', baked['status'] == 'complete'
                  and baked['thickness'] == thickness
                  and rebuilt.thickness_mode == material.thickness_mode
                  and bake._output_thickness(output(rebuilt)) == thickness
                  and core.verify_managed_state(material)['ok']
                  and core.get_stored_manifest(material) == before,
                  bake_report=baked['report_file'])
            clone = obj.copy()
            clone.data = obj.data.copy()
            clone.name = name + 'Rebuilt'
            bpy.context.scene.collection.objects.link(clone)
            clone.material_slots[0].link = 'OBJECT'
            clone.material_slots[0].material = rebuilt
            expected[name]['rebuilt_object'] = clone.name
            if name == 'Positive':
                legacy_report = copy.deepcopy(baked)
                legacy_report.pop('thickness')
                check('legacy_bake_report_auto',
                      not output(bake.rebuild_material(legacy_report)).inputs['Thickness'].is_linked)

    material = bpy.data.objects['Positive'].material_slots[0].material
    original_state = material[core.STATE]
    material.thickness_mode = 'SPHERE'
    reject('mode_edit_read_rejected', lambda: core.get_stored_manifest(material), 'managed_conflict')
    reject('mode_edit_bake_rejected', lambda: bake._material_contract(material), 'managed_conflict')
    material.thickness_mode = 'SLAB'
    tree = material.node_tree
    socket = output(material).inputs['Thickness']
    previous = socket.links[0].from_socket
    tree.links.remove(socket.links[0])
    value = tree.nodes.new('ShaderNodeValue')
    value.outputs[0].default_value = .1
    tree.links.new(value.outputs[0], socket)
    reject('equal_value_foreign_link_rejected', lambda: core.get_stored_manifest(material), 'managed_conflict')
    tree.nodes.remove(value)
    tree.links.new(previous, socket)
    check('protection_restore_no_snapshot_rewrite', core.verify_managed_state(material)['ok']
          and material[core.STATE] == original_state)

    # Unmanaged Principled graph: unknown procedural Thickness cannot become a
    # declared scalar or silently fall back to AUTO during export.
    dynamic = material.copy()
    del dynamic[core.STATE]
    tree = dynamic.node_tree
    socket = output(dynamic).inputs['Thickness']
    tree.links.remove(socket.links[0])
    noise = tree.nodes.new('ShaderNodeTexNoise')
    tree.links.new(noise.outputs['Fac'], socket)
    reject('dynamic_thickness_rejected', lambda: bake._material_contract(dynamic), 'bake_thickness')
    bpy.data.materials.remove(dynamic)
    auto = bpy.data.objects['Auto'].material_slots[0].material
    socket = output(auto).inputs['Thickness']
    previous = socket.default_value
    socket.default_value = .7
    check('unlinked_nonzero_default_remains_auto',
          bake._output_thickness(output(auto)) == {'mode': 'AUTO', 'space': 'OBJECT', 'value': None})
    socket.default_value = previous
    (destination / 'fixtures.json').write_text(json.dumps(expected, indent=2), encoding='utf-8')
    bpy.ops.wm.save_as_mainfile(filepath=str(destination / 'optics.blend'))
    inspect_fixture(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Absolute NEW output directory')
    parser.add_argument('--verify-fixtures', help='Absolute earlier build-output directory for independent reopen')
    raw = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
    args = parser.parse_args(raw)
    destination = Path(args.output)
    if not bpy.app.background or bpy.app.version < (5, 2, 0):
        raise RuntimeError('Requires background Blender 5.2 or newer')
    if not destination.is_absolute() or destination.exists():
        raise ValueError('--output must be an absolute NEW directory')
    if args.verify_fixtures and not Path(args.verify_fixtures).is_absolute():
        raise ValueError('--verify-fixtures must be absolute')
    destination.mkdir(parents=True, exist_ok=False)
    report = {'status': 'fail', 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'checks': [],
              'scope': 'Contract/graph persistence and 32px disk bakes; no visual equivalence or production performance claim'}
    try:
        if args.verify_fixtures:
            inspect_fixture(Path(args.verify_fixtures))
        else:
            build(destination)
        report['status'] = 'pass'
    except Exception as error:
        report['error'] = {'message': str(error), 'traceback': traceback.format_exc()}
        raise
    finally:
        (destination / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('OPTICS_REPORT', destination / 'report.json', flush=True)

