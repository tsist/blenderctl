# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-only fixture/oracle worker. Called by run_blender_smoke.py.

Never connects to a GUI. Creates original procedural texture and Cube fixture.
"""
import hashlib
import json
from pathlib import Path
import sys
import bpy

args = sys.argv[sys.argv.index('--') + 1:]
mode, out_text, root_text = args[:3]
out, root = Path(out_text), Path(root_text)


def write(name, value):
    (out / name).write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


if mode == 'fixture':
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    cube.name = 'ReleaseCube'
    assert cube.data.uv_layers
    cube.data.uv_layers.active.name = 'UVMap'
    image = bpy.data.images.new('ReleaseTexture', width=8, height=8, alpha=True)
    image.pixels = [channel for y in range(8) for x in range(8)
                    for channel in ((0.8, 0.2, 0.05, 1) if (x+y) % 2 else (0.1, 0.4, 0.8, 1))]
    image.filepath_raw = str(out / 'texture.png')
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)
    bpy.context.scene.name = 'ReleaseScene'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'source.blend'))
    texture = {'file': str(out / 'texture.png'), 'expected_sha256': digest(out / 'texture.png')}
    manifest = {'schema_version': '1.1', 'context': {'scene': 'ReleaseScene', 'view_layer': 'ViewLayer', 'frame': 1},
                'target': {'object': 'ReleaseCube', 'uv_layer': 'UVMap'},
                'material': {'id': 'ReleaseMaterial', 'name': 'Release Material', 'template': 'pbr_layers_v2'},
                'layers': [{'id': 'Base', 'name': 'Original checker', 'channels': {'base_color': dict(texture, color_space='sRGB')},
                            'values': {'roughness': 0.45}}],
                'preview': {'engine': 'CYCLES', 'device': {'backend': 'CPU'}, 'width': 64, 'height': 64,
                            'samples': 4, 'denoise': False, 'views': [{'id': 'Front', 'azimuth': 45, 'elevation': 20}]}}
    write('request.json', {'schema_version': '1.0', 'command': 'material.run', 'params': {
        'file': str(out / 'source.blend'), 'expected_sha256': digest(out / 'source.blend'),
        'manifest': manifest, 'resources': [texture]}})
    write('fixture.json', {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                          'source_sha256': digest(out / 'source.blend'), 'texture_sha256': texture['expected_sha256']})
elif mode == 'oracle':
    result = json.loads((out / 'cli-result.json').read_text(encoding='utf-8'))
    candidate = Path(result['data']['candidate'])
    bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
    cube = bpy.data.objects['ReleaseCube']
    assert len(cube.data.vertices) == 8 and len(cube.data.polygons) == 6
    assert cube.data.uv_layers.get('UVMap')
    material = cube.material_slots[0].material
    assert material is not None and material.use_nodes
    assert material.node_tree.nodes
    image_paths = set()
    def paths(value):
        if isinstance(value, str) and value.lower().endswith('.png'): image_paths.add(value)
        elif isinstance(value, dict):
            for child in value.values(): paths(child)
        elif isinstance(value, list):
            for child in value: paths(child)
    paths(result['data'])
    paths(json.loads(Path(result['data']['report']).read_text(encoding='utf-8')))
    decoded = []
    for path in sorted(image_paths):
        image = bpy.data.images.load(path, check_existing=False)
        assert image.size[0] > 0 and image.size[1] > 0
        assert len(image.pixels) == image.size[0] * image.size[1] * 4
        decoded.append({'file': path, 'size': list(image.size), 'decoded': True})
        bpy.data.images.remove(image)
    assert any(row['size'] == [64, 64] for row in decoded), decoded
    sys.path.insert(0, str(root / 'tools'))
    import material_workflow_addon as addon
    addon.register()
    addon.unregister()
    write('oracle.json', {'ok': True, 'candidate': str(candidate), 'candidate_sha256': digest(candidate),
                         'vertices': 8, 'faces': 6, 'uv_preserved': True, 'material_nodes': True,
                         'images': decoded, 'addon_register_unregister': True})
else:
    raise ValueError('Unknown worker mode: ' + mode)
