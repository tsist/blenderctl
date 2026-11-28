# SPDX-License-Identifier: GPL-3.0-or-later
"""One host-owned fresh Blender stage; no audit directory or commercial runtime."""
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent)]


def write(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    raw = Path(args[0]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != args[1]:
        raise RuntimeError('Image stage binding digest differs')
    binding = json.loads(raw)
    profile = binding.get('profile', 'intersection-image.v1')
    if profile not in ('intersection-image.v1', 'selected-mesh-image.v1'):
        raise RuntimeError('Unsupported fixed image profile')
    sys.pycache_prefix = binding['empty_pycache']
    import bpy
    root = Path(binding['run_root'])
    stage = Path(binding['stage_dir'])
    if profile == 'selected-mesh-image.v1':
        write(stage / 'STAGE-OBSERVATION.json', dict(mode=binding['mode'],
            python_version=list(sys.version_info[:3]), optimization=sys.flags.optimize, executable=sys.executable))
        if sys.flags.optimize != binding['optimize']:
            raise RuntimeError('Target optimization differs from parent binding')
    if binding['mode'] == 'identity':
        import numpy as np
        write(stage / 'RUNTIME.json', dict(python=sys.version, numpy=np.__version__,
            blender=bpy.app.version_string, blender_build=bpy.app.build_hash.decode('ascii')))
    elif binding['mode'] == 'export':
        if profile == 'selected-mesh-image.v1':
            from toon_selected_image_saved_export import export_saved
            value = export_saved(binding['export_request'], str(root / 'export'), checkpoint=lambda: None)
        else:
            from toon_image_saved_export import export_saved
            value = export_saved(binding['params'], str(root / 'export'), checkpoint=lambda: None)
        write(stage / 'EXPORT-RETURN.json', value)
    elif binding['mode'] == 'numeric':
        if profile == 'selected-mesh-image.v1':
            from toon_image_backend.selected_image_runner import run
            output = root / 'bundle'
        else:
            from toon_image_backend.intersection_image_runner import run
            output = root / 'result'
        value = run(binding['request_path'], output,
            expected_request_sha256=binding['request_sha256'], allowed_roots=[root])
        write(stage / 'NUMERIC-RETURN.json', dict(ok=value['ok'], receipt_sha256=
            hashlib.sha256((output / 'receipt.json').read_bytes()).hexdigest()))
    else:
        raise RuntimeError('Unsupported image stage')


if __name__ == '__main__':
    main()
