"""Narrow Linux read profile for trusted synthetic 5.3 armature fixtures."""
import os

import bpy
from _blendfile_header import BlendFileHeader, BlendHeaderError
from protocol import Failure


def unsupported(message):
    raise Failure("UNSUPPORTED", "Linux rig fixture: " + message)


def open_fixture(path):
    # Inspect the main-file block directory before Blender can follow a library
    # link. Only ordinary standalone armature/mesh/action fixture IDs qualify.
    allowed = {b'GLOB', b'DNA1', b'DATA', b'ENDB', b'TEST', b'REND',
               *(s.ljust(4, b'\0') for s in (b'AR', b'OB', b'AC', b'ME', b'GR',
                                              b'WM', b'SN', b'SC', b'WS'))}
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
    if (bpy.utils.blend_paths(absolute=True, packed=True) or bpy.data.libraries
            or any(s.sequence_editor and len(s.sequence_editor.strips) for s in bpy.data.scenes)
            or any(o.type not in {'ARMATURE', 'MESH', 'EMPTY'} or o.particle_systems
                   or any(m.type != 'ARMATURE' for m in o.modifiers) for o in bpy.data.objects)):
        unsupported("only self-contained armature and mesh fixtures are supported")
    import drivers
    if any(getattr(item, 'animation_data', None) and item.animation_data.drivers
           for item, _ in drivers.owners()):
        unsupported("driver evaluation is outside the fixture scope")
