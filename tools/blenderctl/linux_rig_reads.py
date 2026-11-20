"""Narrow Linux read profiles for trusted synthetic Blender 5.3 fixtures."""
import os

import bpy
from _blendfile_header import BlendFileHeader, BlendHeaderError
from protocol import Failure


def unsupported(message):
    raise Failure("UNSUPPORTED", "Linux rig fixture: " + message)


def _open_guarded_fixture(path, allowed_ids, unsupported):
    # Inspect the main-file block directory before Blender can follow a library
    # link. Only the profile's explicitly allowed local ID types qualify.
    allowed = {b'GLOB', b'DNA1', b'DATA', b'ENDB', b'TEST', b'REND',
               *(s.ljust(4, b'\0') for s in allowed_ids)}
    if bpy.app.version[:3] != (5, 3, 0):
        unsupported("requires Blender 5.3.0")
    with open(path, 'rb') as stream:
        end = os.fstat(stream.fileno()).st_size
        if end > 64 * 1024 * 1024:
            unsupported("file exceeds 64 MiB")
        try:
            header = BlendFileHeader(stream)
        except (BlendHeaderError, ValueError):
            unsupported("uncompressed Blender 5.3 file required")
        if header.version != 503 or header.file_format_version != 1:
            unsupported("requires a native uncompressed 5.3 fixture")
        fmt = header.create_block_header_struct()
        while True:
            data = stream.read(fmt.size)
            if len(data) != fmt.size:
                unsupported("invalid block directory")
            block = fmt.parse(data)
            if block.code not in allowed or block.len < 0 or stream.tell() + block.len > end:
                unsupported("external, unknown or malformed data block")
            if block.code == b'ENDB':
                if block.len or stream.tell() != end:
                    unsupported("invalid end block")
                break
            stream.seek(block.len, os.SEEK_CUR)
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)


def open_fixture(path):
    _open_guarded_fixture(path, (b'AR', b'OB', b'AC', b'ME', b'GR', b'WM', b'SN', b'SC', b'WS'), unsupported)
    if (bpy.utils.blend_paths(absolute=True, packed=True) or bpy.data.libraries
            or any(s.sequence_editor and len(s.sequence_editor.strips) for s in bpy.data.scenes)
            or any(o.type not in {'ARMATURE', 'MESH', 'EMPTY'} or o.particle_systems
                   or any(m.type != 'ARMATURE' for m in o.modifiers) for o in bpy.data.objects)):
        unsupported("only self-contained armature and mesh fixtures are supported")
    import drivers
    if any(getattr(item, 'animation_data', None) and item.animation_data.drivers
           for item, _ in drivers.owners()):
        unsupported("driver evaluation is outside the fixture scope")


def open_scene_fixture(path):
    def reject(message):
        raise Failure('UNSUPPORTED', 'Linux scene fixture: ' + message)
    _open_guarded_fixture(path, (b'OB', b'ME', b'GR', b'WM', b'SN', b'SC', b'WS',
                                 b'CA', b'LA', b'WO'), reject)
    if (bpy.utils.blend_paths(absolute=True, packed=True) or bpy.data.libraries
            or any(scene.sequence_editor and len(scene.sequence_editor.strips) for scene in bpy.data.scenes)
            or any(getattr(scene, 'compositing_node_group', None) or getattr(scene, 'compositor_effects', None) for scene in bpy.data.scenes)
            or any(o.type not in {'MESH', 'EMPTY', 'CAMERA', 'LIGHT'}
                   or o.particle_systems or o.modifiers or o.constraints for o in bpy.data.objects)):
        reject('only static, self-contained mesh, empty, camera and light objects are supported')
    import drivers
    if any(getattr(item, 'animation_data', None) for item, _ in drivers.owners()):
        reject('animation and driver evaluation are outside the static fixture scope')
