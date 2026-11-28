# SPDX-License-Identifier: GPL-3.0-or-later
"""One fixed paint-aware Blender stage under the existing owned ProcessTree."""
from pathlib import Path
import hashlib,json,sys
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parent)]

def write(path,value):
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')

def main():
    args=sys.argv[sys.argv.index('--')+1:]
    if len(args)!=2:raise RuntimeError('Fixed binding path and SHA required')
    path=Path(args[0]);raw=path.read_bytes()
    if len(raw)>8*1024**2 or hashlib.sha256(raw).hexdigest()!=args[1]:raise RuntimeError('Stage binding extent/SHA differs')
    b=json.loads(raw)
    if b['profile']!='painted-mesh-image.v1':raise RuntimeError('Explicit painted profile required')
    if sys.flags.optimize!=b['optimize']:raise RuntimeError('Stage optimization differs')
    sys.pycache_prefix=b['empty_pycache']
    root=Path(b['run_root']);stage=Path(b['stage_dir'])
    write(stage/'STAGE-OBSERVATION.json',dict(mode=b['mode'],python_version=list(sys.version_info[:3]),optimization=sys.flags.optimize,executable=sys.executable))
    import bpy
    if b['mode']=='identity':
        import numpy as np
        write(stage/'RUNTIME.json',dict(python=sys.version,numpy=np.__version__,blender=bpy.app.version_string,blender_build=bpy.app.build_hash.decode('ascii')))
    elif b['mode']=='export':
        from toon_painted_image_saved_export import export_saved
        value=export_saved(b['export_request'],str(root/'export'),checkpoint=lambda:None)
        write(stage/'EXPORT-RETURN.json',value)
    elif b['mode']=='numeric':
        request_path=Path(b['request_path']);data=request_path.read_bytes()
        if len(data)>1024**2 or hashlib.sha256(data).hexdigest()!=b['request_sha256']:raise RuntimeError('Numerical request identity differs')
        req=json.loads(data)
        if req['schema']=='toon-painted-authored-image-request.v1':
            from toon_image_backend.painted_image_runner import run
        elif req['schema']=='toon-painted-intersection-image-request.v1':
            from toon_image_backend.painted_intersection_runner import run
        else:raise RuntimeError('Explicit supported painted producer required')
        value=run(str(request_path),str(root/'bundle'),expected_request_sha256=b['request_sha256'],allowed_roots=[str(root)])
        write(stage/'NUMERIC-RETURN.json',dict(ok=value['ok'],receipt_sha256=hashlib.sha256((root/'bundle/receipt.json').read_bytes()).hexdigest()))
    else:raise RuntimeError('Unsupported fixed painted stage')

if __name__=='__main__':main()
