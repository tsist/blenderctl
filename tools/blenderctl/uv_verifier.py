"""Independent read-only Blender process for saved UV candidates."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import atomic_json,digest

def verify(path):
    import bpy
    from uv_workflow_addon import core
    config=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    report={'status':'failed','runtime':{'version':bpy.app.version_string,'build':bpy.app.build_hash.decode()}}
    try:
        from uv_workflow_contract import implementation_sha256
        if implementation_sha256()!=config['implementation_sha256']:raise ValueError('UV implementation changed')
        runtime={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'binary_sha256':digest(bpy.app.binary_path)}
        if runtime!=config['runtime']:raise ValueError('Blender runtime changed')
        if digest(config['candidate']['file'])!=config['candidate']['sha256']:raise ValueError('Candidate bytes changed')
        bpy.ops.wm.open_mainfile(filepath=config['candidate']['file'],load_ui=False,use_scripts=False)
        m=dict(config.get('inspection_manifest',config['manifest']));m['operation']='inspect'
        scene=bpy.data.scenes[m['scene']];bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[m['view_layer']]
        actual=core.execute(m)
        if actual!=config['inspection']:raise ValueError('Saved UV state or target identity differs')
        objects=[scene.objects[n] for n in m['objects']]
        if core.protection(objects)!=config['protection']:raise ValueError('Saved target data differs')
        if config['operation']=='transfer':
            source=config.get('read_only_sources')
            if not source:raise ValueError('Transfer source protection is required')
            expected_names=sorted({r['source_object'] for r in config['manifest']['transfer'].values()})
            if source['objects']!=expected_names:raise ValueError('Transfer source set differs')
            source_objects=[scene.objects[n] for n in source['objects']]
            if core.protection(source_objects)!=source['protection']:raise ValueError('Saved read-only source UV, pins, seams or render data differs')
            active={o.name:o.data.uv_layers.active.name if o.data.uv_layers.active else None for o in source_objects}
            if active!=source['active']:raise ValueError('Saved read-only source active UV differs')
            report['transfer_sources']='exact'
        if 'non_target_sha256' in config:
            from uv_saved_protection import scene_fingerprint
            if scene_fingerprint(exclude_objects=m['objects'],persistence_exceptions=config.get('persistence_exceptions',[]))!=config['non_target_sha256']:
                raise ValueError('Non-target data or materials differ after reopening')
        report.update(status='passed',state='exact',target='exact',non_target='passed' if 'non_target_sha256' in config else 'not_checked',visual='not_reviewed')
        if 'udim' in actual:
            from uv_workflow_contract import udim_report_fields
            report.update(udim_report_fields(actual['udim']))
    except Exception as e:report['error']=str(e)
    atomic_json(config['output'],report)
    return 0 if report['status']=='passed' else 8

if __name__=='__main__':sys.exit(verify(sys.argv[sys.argv.index('--')+1]))
