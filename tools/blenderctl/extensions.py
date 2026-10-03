# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapter inventory and explicitly selected local Python execution modes."""
from pathlib import Path
import bpy
from protocol import Failure,atomic_json,digest,read_json
from exchange_contract import normalize
def prepare(params,job):
    import types,sys
    from contextlib import ExitStack
    from filesystem import FileGuard
    from exchange_contract import addon_documents
    import scenes,modeling
    from exchange import snapshot,compare_geometry,save_candidate
    spec=normalize(params['manifest'],'prepare');files=addon_documents(bpy.app.binary_path)
    bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.name='Extension'
    with ExitStack() as stack:
        for f in files:
            g=stack.enter_context(FileGuard(f['file']))
            if g.sha256()!=f['expected_sha256']:raise Failure('CONFLICT','Installed adapter source changed; review before updating pin')
        package=types.ModuleType('_blenderctl_extra_mesh');package.__path__=[];sys.modules[package.__name__]=package
        for f in files:
            name=package.__name__+'.'+Path(f['file']).stem;m=types.ModuleType(name);m.__package__=package.__name__;m.__file__=f['file'];sys.modules[name]=m;exec(compile(Path(f['file']).read_bytes(),f['file'],'exec'),m.__dict__)
        cls=sys.modules[package.__name__+'.add_mesh_star'].AddStar;bpy.utils.register_class(cls)
        try:modeling.finished(bpy.ops.mesh.primitive_star_add(points=spec['points'],outer_radius=spec['outer_radius'],innter_radius=spec['inner_radius'],height=spec['height'],change=False,align='WORLD',location=(0,0,0),rotation=(0,0,0)))
        finally:bpy.utils.unregister_class(cls)
        scenes.named(bpy.context.object,spec['name']);before=snapshot(1);candidate=save_candidate(job);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);after=snapshot(1);check=compare_geometry(before,after)
        if not check['ok']:raise Failure('VALIDATION_FAILED','Plugin candidate reopen differs')
    report={'extension_report_version':'1.0','adapter':'extra_mesh_star_v1','settings':spec,'reviewed_package_version':'0.4.1','source_files':files,'candidate':str(candidate),'candidate_sha256':digest(candidate),'checks':[check],'reopen':'pass','scope':'Only the pinned star operator and interface module loaded; no full add-on registration or persistent preference change'};atomic_json(job/'extension-report.json',report);return report
def inspect(params,job):
    from exchange import formats
    from worker import doctor
    from review_contract import ACK,ACK_ISOLATED,LIMITS
    return {**formats(),'disk_candidates':doctor()['installed_candidates'],'third_party_execution':False,'script_contract':'Explicit trust and exact script SHA; omitted isolation selects legacy current-user execution. WINDOWS_APPCONTAINER_V1 selects Windows OS isolation with no fallback.', 'script_isolation':{'profile':'WINDOWS_APPCONTAINER_V1','trust':'execute_local_python_in_appcontainer','acknowledgment':ACK_ISOLATED,'status':'experimental','platform':'Windows only; locally verified with Blender 5.2.1','lpac':False,'network_capabilities':[],'input_access':'input_files maps declared resource paths to staged copies','limitations':'Windows AppContainer/AAP-readable resources and per-job profile storage remain accessible; no VM or hard disk/VRAM quota; abnormal supervisor termination may leave a profile for explicit cleanup'},'review_contract':{'commands':['extension.review','extension.approve','extension.run'],'acknowledgment':ACK,'receipt_optional':True,'reuse':'exact_request_only','limitations':LIMITS,'scope':'Approval is a local reviewer assertion; preserve independently recorded receipt SHA. Imports are static hints only; additional inputs and permissions cannot be proven complete.'}}
def run(params,job):
    import hashlib
    from contextlib import ExitStack
    from reviews import verify_run
    spec=normalize(params['manifest'],'run');script=Path(spec['script']['file'])
    if 'isolation' in spec:raise Failure('UNSUPPORTED','Isolated scripts must use the host AppContainer launcher; no local execution fallback')
    if script.suffix.lower()!='.py':raise Failure('INVALID_REQUEST','Trusted script must be .py')
    namespace={'__name__':'__main__','__file__':str(script),'job_directory':str(job),'parameters':spec['params'],'result':None}
    with ExitStack() as stack:
        authorization=verify_run(params,bpy.app.binary_path,stack)
        raw=script.read_bytes();observed=hashlib.sha256(raw).hexdigest()
        if observed!=spec['script']['expected_sha256']:raise Failure('CONFLICT','Executed script bytes differ from pinned source')
        exec(compile(raw,str(script),'exec'),namespace,namespace)
    outputs=[]
    for name in spec['outputs']:
        p=job/name
        if p.is_symlink() or p.resolve().parent!=job.resolve() or not p.is_file():raise Failure('VALIDATION_FAILED','Declared script output missing or escapes job directory')
        if p.suffix=='.json':read_json(p)
        outputs.append({'file':str(p),'sha256':digest(p),'bytes':p.stat().st_size})
    report={'extension_report_version':'1.0','script':spec['script'],'outputs':outputs,'result':namespace.get('result'),'authorization':authorization,'script_sha256_observed':observed,'execution':'trusted local Python, no OS security sandbox; declared outputs do not constrain arbitrary script side effects'};atomic_json(job/'extension-report.json',report);return report
