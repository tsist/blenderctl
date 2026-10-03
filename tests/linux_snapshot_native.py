# SPDX-License-Identifier: GPL-3.0-or-later
"""Native snapshot fixture and readback oracle (not imported by unittest)."""
import hashlib
import json
from pathlib import Path
import sys
import bpy

arguments = sys.argv[sys.argv.index('--') + 1:]
mode, folder, repository = arguments[:3]
folder, repository = Path(folder), Path(repository)
sys.path.insert(0, str(repository / 'tests'))
from test_linux_snapshot import request_for, descriptor

if mode.startswith('fixture'):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.object.name = 'Cube'
    bpy.context.scene.name = 'Scene'
    for name, relative, color in [('AbsoluteTexture', False, (0.7, 0.1, 0.2, 1)), ('RelativeTexture', True, (0.1, 0.2, 0.7, 1))]:
        path = folder / (name + '.png')
        image = bpy.data.images.new(name, width=8, height=8, alpha=True)
        image.pixels = list(color) * 64
        image.filepath_raw = str(path); image.file_format = 'PNG'; image.save()
        bpy.data.images.remove(image)
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = name; image.use_fake_user = True
        image.filepath = '//' + path.name if relative else str(path)
    resources = [descriptor(folder / (name + '.png')) for name in ('AbsoluteTexture', 'RelativeTexture')]
    system_font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if system_font.is_file():
        font_path = folder / 'fixture-font.ttf'; font_path.write_bytes(system_font.read_bytes())
        font = bpy.data.fonts.load(str(font_path)); font.use_fake_user = True
        font.filepath = '//fixture-font.ttf'; resources.append(descriptor(font_path))
    if mode == 'fixture-driver':
        curve = bpy.data.objects['Cube'].driver_add('location', 0)
        curve.driver.expression = 'frame'
    if mode == 'fixture-sequence':
        bpy.data.images['RelativeTexture'].source = 'SEQUENCE'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(folder / 'source.blend'), relative_remap=False)
    request = request_for(folder / 'source.blend', folder / 'AbsoluteTexture.png')
    request['params']['resources'] = resources
    (folder / 'request.json').write_text(json.dumps(request), encoding='utf-8')
elif mode == 'oracle':
    request = json.loads((folder / 'request.json').read_text())
    bpy.ops.wm.open_mainfile(filepath=request['params']['file'], load_ui=False, use_scripts=False)
    resources = {row['file']: row['expected_sha256'] for row in request['params']['resources']}
    for item in [*bpy.data.images, *bpy.data.fonts]:
        if getattr(item, 'packed_file', None) or item.filepath == '<builtin>' or getattr(item, 'source', None) == 'GENERATED':
            continue
        assert item.filepath.startswith('//'), item.filepath
        target = str(Path(bpy.path.abspath(item.filepath)).resolve())
        assert Path(target).is_relative_to(folder), target
        assert hashlib.sha256(Path(target).read_bytes()).hexdigest() == resources[target]
    for name in ('AbsoluteTexture', 'RelativeTexture'):
        image = bpy.data.images[name]; image.reload()
        assert list(image.size) == [8, 8], list(image.size)
        assert len(image.pixels) == 8 * 8 * 4
elif mode == 'archive':
    manifest = json.loads((folder / 'archive-manifest.json').read_text())
    candidate = folder / manifest['candidate']
    hashes = {str(folder / row['path']): row['sha256'] for row in manifest['files']}
    for path, expected in hashes.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, path
    bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
    references = []
    for item in [*bpy.data.images, *bpy.data.fonts]:
        if getattr(item, 'packed_file', None) or item.filepath == '<builtin>' or getattr(item, 'source', None) == 'GENERATED':
            continue
        assert item.filepath.startswith('//'), item.filepath
        path = str(Path(bpy.path.abspath(item.filepath)).resolve())
        assert Path(path).is_relative_to(candidate.parent), path
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == hashes[path]
        references.append({'name': item.name, 'path': path})
    decoded = []
    for row in manifest['files']:
        if row['path'].endswith('.png'):
            path = folder / row['path']
            image = bpy.data.images.load(str(path), check_existing=False)
            assert image.size[0] > 0 and image.size[1] > 0, path
            assert len(image.pixels) == image.size[0] * image.size[1] * 4, path
            decoded.append({'file': row['path'], 'size': list(image.size)})
            bpy.data.images.remove(image)
    report = {'ok': True, 'candidate': str(candidate), 'archive_hashes_verified': len(hashes), 'references': references, 'decoded_images': decoded}
    if len(arguments) > 3:
        with Path(arguments[3]).open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
    else:
        print(json.dumps(report))
else:
    raise ValueError(mode)
