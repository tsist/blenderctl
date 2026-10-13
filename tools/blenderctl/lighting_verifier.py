"""Fresh Blender process reader; never saves a candidate."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import atomic_json,digest
from lighting_identity import implementation_sha256
from geometry_verifier import near
def verify(path):
    import bpy
    from lighting_workflow_addon import core
    config=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    report={'status':'failed','blender':bpy.app.version_string,'build':bpy.app.build_hash.decode()}
    try:
        if implementation_sha256()!=config['implementation_sha256']:raise ValueError('Implementation changed')
        runtime={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'binary_sha256':digest(bpy.app.binary_path)}
        if runtime!=config['runtime']:raise ValueError('Blender runtime differs')
        if digest(config['candidate']['file'])!=config['candidate']['sha256']:raise ValueError('Candidate changed')
        bpy.ops.wm.open_mainfile(filepath=config['candidate']['file'],load_ui=False,use_scripts=False)
        scene=bpy.data.scenes[config['scene']];bpy.context.window.scene=scene
        bpy.context.window.view_layer=scene.view_layers[config['view_layer']]
        scene.frame_set(config['frame']);bpy.context.view_layer.update()
        inspection=json.loads(json.dumps(core.inspect(config['scene'],config['rig_id'])))
        mismatch=near(config['inspection'],inspection)
        if mismatch:raise ValueError('Saved lighting differs: '+mismatch)
        actual=json.loads(json.dumps(core.snapshot_protection(config['scene'],excluded_names=config.get('excluded_names',[]))))
        mismatch=near(config['protection'],actual)
        if mismatch:raise ValueError('Reopened non-target data differs: '+mismatch)
        report.update(status='passed',inspection=inspection,non_target_protection='passed')
    except Exception as e:report['error']=str(e)
    atomic_json(config['output'],report)
    return 0 if report['status']=='passed' else 8
if __name__=='__main__':sys.exit(verify(sys.argv[sys.argv.index('--')+1]))
