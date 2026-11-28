# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed capture-only Blender stage or fresh CPU-only Python stage."""
from pathlib import Path
import hashlib
import json
import sys
import time
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parent)]

def need(value,message):
    if not value:raise RuntimeError(message)

def read_bytes(ref,maximum):
    path=Path(ref['file']);need(path.is_absolute(),'Absolute bound path required')
    with path.open('rb') as stream:raw=stream.read(maximum+1)
    need(len(raw)<=maximum and hashlib.sha256(raw).hexdigest()==ref['sha256'],'Bound document SHA/extent differs')
    if 'bytes' in ref:need(len(raw)==ref['bytes'],'Bound byte extent differs')
    return raw

def read(ref,maximum):
    from toon_cpu_image_evidence import strict_json
    return strict_json(read_bytes(ref,maximum))

def main():
    args=sys.argv[sys.argv.index('--')+1:];need(len(args)==2,'Exact fixed binding path/SHA required')
    # -B disables writes, but can still consume a matching .pyc. Only trusted
    # stdlib runs until the bound per-stage empty cache has been selected.
    raw=read_bytes(dict(file=args[0],sha256=args[1]),8*1024**2)
    b=json.loads(raw)
    sys.pycache_prefix=b['empty_pycache']
    from toon_cpu_image_evidence import strict_json,write_document
    b=strict_json(raw)
    need(b['schema']=='toon-cpu-image-stage-binding.v1' and b['profile']=='cpu-boundary-image.v1','Explicit CPU profile required')
    root=Path(b['run_root']);stage=Path(b['stage_dir'])
    write_document(stage/'STAGE-OBSERVATION.json',dict(mode=b['mode'],python_version=list(sys.version_info[:3]),executable=sys.executable,
        pure_line_output=True,scene_render_called=False))
    if b['mode']=='export':
        from toon_painted_image_saved_export import export_saved
        value=export_saved(b['export_request'],str(root/'export'),checkpoint=lambda:None)
        write_document(stage/'EXPORT-RETURN.json',value)
        return
    need(b['mode']=='numeric','Fixed supported stage required')
    expected=read(b['expectation_ref'],8*1024**2);request=read(b['request_ref'],1048576)
    need(expected['request_ref']==b['request_ref'],'Expected numerical request differs')
    need(not (root/'bundle').exists(),'Bundle exists before numerical work')
    from toon_image_cpu import prepare_frame,render_frame,implementation_sha256
    from toon_cpu_image_evidence import produce_bundle
    need(implementation_sha256()==expected['provider_implementation_sha256'],'Actual CPU provider identity differs')
    budgets=request.get('budgets',{});started=time.perf_counter();deadline=started+budgets.get('binding',{}).get('max_seconds',300)
    def check():need(time.perf_counter()<=deadline,'Cooperative phase time budget exceeded')
    frame=prepare_frame(b['request_ref']['file'],expected_request_sha256=b['request_ref']['sha256'],allowed_roots=[str(root)],checkpoint=check)
    try:
        need(list(frame.image_shape)==expected['shape'],'Prepared output shape differs')
        record=dict(snapshot_key=frame.snapshot_key,source_revision=frame.source_revision,style_revision=frame.style_revision,
            settings=frame.settings,counts=frame.counts,prepare_metrics=frame.prepare_metrics)
        deadline=time.perf_counter()+budgets.get('render',{}).get('max_seconds',300)
        images,metrics=render_frame(frame,expected_snapshot=frame.snapshot_key,checkpoint=check)
        receipt=produce_bundle(root/'bundle',images,record,metrics,expectation_ref=b['expectation_ref'],expectation=expected,checkpoint=check)
        need(receipt['file']=='receipt.json','Fixed bundle receipt role required')
        receipt=dict(receipt,file=str(root/'bundle/receipt.json'))
    finally:frame.close()
    write_document(stage/'NUMERIC-RETURN.json',dict(ok=True,receipt=receipt))

if __name__=='__main__':main()
