"""Fresh Blender reader; reads only an owned candidate and writes its witness."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bpy
from protocol import atomic_json, digest, read_json
from modeling_identity import implementation_sha256
from modeling_workflow_addon import core

request = read_json(sys.argv[sys.argv.index('--') + 1])
try:
    if not bpy.app.background:
        raise RuntimeError('Verifier requires an isolated background process')
    if implementation_sha256() != request['implementation_sha256']:
        raise RuntimeError('Modeling implementation changed before independent reader')
    candidate = request['candidate']
    if digest(candidate['file']) != candidate['sha256']:
        raise RuntimeError('Candidate SHA differs before independent reader')
    observed = core.verify_candidate(candidate['file'], request['content'],
                                     studio_fbx=request.get('studio_fbx'), native_normals=request.get('native_normals'))
    decoded = []
    for row in request.get('previews', []):
        if digest(row['file']) != row['sha256']:
            raise RuntimeError('Preview SHA changed')
        image = bpy.data.images.load(row['file'], check_existing=False)
        if list(image.size) != [row['width'], row['height']]:
            raise RuntimeError('Decoded preview size differs')
        decoded.append({'view': row['view'], 'file': row['file'], 'size': list(image.size), 'status': 'passed'})
        bpy.data.images.remove(image)
    if digest(candidate['file']) != candidate['sha256']:
        raise RuntimeError('Independent reader changed candidate bytes')
    fbx = {key: request[key] for key in ('studio_fbx', 'native_normals')} if 'studio_fbx' in request else {}
    atomic_json(request['output'], {'status': 'passed', 'process': 'fresh_process',
                'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                'candidate': candidate, 'content': observed, 'decoded_previews': decoded, **fbx})
except Exception as exc:
    atomic_json(request['output'], {'status': 'failed', 'process': 'fresh_process', 'error': str(exc)})
    raise
