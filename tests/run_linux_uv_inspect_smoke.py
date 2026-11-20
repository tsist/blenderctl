import sys, subprocess, hashlib, json
from pathlib import Path
import tempfile
cli=Path(__file__).resolve().parents[1]/'tools/blenderctl/cli.py'
blender=Path(sys.argv[1]).resolve()
temp=tempfile.TemporaryDirectory(prefix='uv-test-',dir='/tmp')
d=Path(temp.name); blend=d/'model.blend'
subprocess.run([str(blender),'--background','--factory-startup','--disable-autoexec','--offline-mode','--python-exit-code','23','--python-expr',
                'import bpy; bpy.ops.wm.save_as_mainfile(filepath='+repr(str(blend))+')'],check=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
h=sha(blend)
manifest=dict(version='1.0',operation='inspect',scene='Scene',view_layer='ViewLayer',objects=['Cube'])
base=dict(file=str(blend),expected_sha256=h,resources=[],manifest=manifest)
def read(params,title,code):
    (d/'params.json').write_text(json.dumps(params))
    call=subprocess.run([sys.executable,str(cli),'--blender',str(blender),'--jobs-dir',str(d/'jobs'),'uv','inspect','--params',str(d/'params.json')],capture_output=True,text=True)
    r=json.loads(call.stdout); (d/(title+'.json')).write_text(call.stdout)
    assert (not call.returncode and r['ok']) if code is None else (call.returncode and r['error']['code']==code),r.get('error')
    print(title,code or 'OK')
    return r
r=read(base,'positive',None); data=r['data'];obj=data['inspection']['objects'][0]
assert (obj['name'],obj['vertices'],obj['faces'],obj['layers'],obj['active_render'])==('Cube',8,6,['UVMap'],['UVMap'])
assert data['source_unchanged'] and data['source_sha256']==h and str(cli.with_name('linux_file_guard.py')) in data['input_hashes']
report=json.loads(Path(data['report']['file']).read_text());assert report['checks']['saves']=='none' and report['quality_acceptance']=='not_applicable_read_only'
read(dict(base,expected_sha256='0'*64),'wrong-sha','CONFLICT')
read(dict(base,manifest=dict(manifest,objects=['Missing'])),'missing-object','NOT_FOUND')
read(dict(base,resources=[dict(file=str(blend),expected_sha256=h)]),'resources','UNSUPPORTED')
read(dict(base,manifest=dict(manifest,diagnostics=True)),'extra-inspection','UNSUPPORTED')
(d/'link.blend').symlink_to(blend);read(dict(base,file=str(d/'link.blend')),'symlink','CONFLICT')
assert sha(blend)==h and not list((d/'jobs').rglob('*.blend'))
print('Guarded inputs:',len(data['input_hashes']),'Runtime:',report['runtime'],'Source unchanged:',h)
