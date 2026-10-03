# SPDX-License-Identifier: GPL-3.0-or-later
"""Open disk snapshots and produce separately verified current-version copies."""
import bpy,os,sys
from pathlib import Path
from protocol import Failure,atomic_json,digest
import inspection

def startup_profile():
    from execution_contract import disk_state
    root=os.environ.get('BLENDER_USER_RESOURCES')
    redirected=bool(root) and all(canonical(bpy.utils.user_resource(k)).startswith(canonical(root)+os.sep) for k in ('CONFIG','SCRIPTS','EXTENSIONS','DATAFILES'))
    return {'id':'BACKGROUND_FACTORY_DISK_V1','background':bpy.app.background,'factory_startup':'--factory-startup' in sys.argv,'user_resources_redirected':redirected,
      'autoexec_enabled':bpy.context.preferences.filepaths.use_scripts_auto_execute,'load_ui':False,'use_scripts':False,
      'online_access':bpy.app.online_access,**disk_state(),
      'extension_policy':'Only separately declared extension adapters; no user startup configuration',
      'scope':'Disk files only; opening is not rendering/animation/dependency-closure acceptance'}

def canonical(p):return os.path.normcase(str(Path(p).resolve()))

def container(path):
    p=Path(path)
    with p.open('rb') as f:magic=f.read(12)
    kind='BLENDER' if magic.startswith(b'BLENDER') else 'ZSTD' if magic.startswith(bytes.fromhex('28b52ffd')) else 'GZIP' if magic.startswith(bytes.fromhex('1f8b')) else 'UNKNOWN'
    return {'kind':kind,'bytes':p.stat().st_size,'compressed':kind in ('ZSTD','GZIP')}

def version_info():
    source=tuple(bpy.data.version);reader=tuple(bpy.app.version)
    # BlendData.version[2] is a file subversion, not bpy.app.version's patch.
    return {'source':list(source),'reader':list(reader),'relation':'older' if source[:2]<reader[:2] else 'newer' if source[:2]>reader[:2] else 'same','version_converted':source[:2]!=reader[:2]}

def opened(params):
    kind=container(params['file'])['kind']
    if kind=='UNKNOWN':raise Failure('VALIDATION_FAILED','Unknown or corrupt blend container')
    if kind=='BLENDER':
        with Path(params['file']).open('rb') as stream:header=stream.read(17)
        # Current 5.2 native writer emits BLENDER17-01v0502. Keep the legacy
        # pointer/endian header too; do not interpret arbitrary trailing bytes.
        if header.startswith(b'BLENDER17-01v') and len(header)==17 and header[13:17].isdigit():version=int(header[13:17])
        elif len(header)>=12 and header[7:8] in (b'_',b'-') and header[8:9] in (b'v',b'V') and header[9:12].isdigit():version=int(header[9:12])
        else:raise Failure('VALIDATION_FAILED','Malformed or unsupported blend version header')
        if version>bpy.app.version[0]*100+bpy.app.version[1]:raise Failure('UNSUPPORTED','Future uncompressed blend header is rejected before native open')
    try:snap=inspection.snapshot(params['file'])
    except RuntimeError as exc:raise Failure('VALIDATION_FAILED','Blender could not open project: '+str(exc)) from exc
    if canonical(bpy.data.filepath)!=canonical(params['file']):raise Failure('VALIDATION_FAILED','Opened project identity differs')
    return snap

def file_paths():
    native={canonical(p) for p in bpy.utils.blend_paths(absolute=True,packed=False,local=False) if p}
    weak={canonical(bpy.path.abspath(x.library_weak_reference.filepath)) for x in inspection.all_ids() if getattr(x,'library_weak_reference',None)}
    # Append provenance is not a live linked library. A real resource with the
    # same filename must still be guarded, so restore all typed input targets.
    strong=set()
    for name in ('images','libraries','sounds','fonts','movieclips','cache_files','volumes'):
        for x in getattr(bpy.data,name,()):
            raw=getattr(x,'filepath','')
            if raw and raw!='<builtin>' and not getattr(x,'packed_file',None) and not getattr(x,'packed_files',[]):
                strong.add(canonical(bpy.path.abspath(raw,library=None if name=='libraries' else x.library)))
    return sorted((native-weak)|strong)

def verify(params,job):
    snap=opened(params);paths=file_paths();declared={canonical(d['file']) for d in params['resources']}
    report={'project_report_version':'1.0','operation':'VERIFY','source':{'file':params['file'],'expected_sha256':params['expected_sha256']},
      'saved_version':list(bpy.data.version),'reader_version':list(bpy.app.version),'profile':startup_profile(),'version_info':version_info(),'container':container(params['file']),
      'external_paths':paths,'undeclared_paths':[p for p in paths if p not in declared],
      'datablocks':len(snap['datablocks']),'validation':{'open':'pass','render':'not_run','animation':'not_run','dependency_closure':'not_claimed'},
      'source_saved':False,'candidate':None,'reopen':None,'path_policy':None,'version_policy':None}
    atomic_json(job/'project-snapshot.json',snap);atomic_json(job/'project-report.json',report)
    return report

def prepare_copy(params,job):
    before=opened(params);version=tuple(bpy.data.version);reader=tuple(bpy.app.version);spec=params['manifest'];versions=version_info()
    if version[:2]>reader[:2] or spec['version_policy']=='SAME_MINOR' and version[:2]!=reader[:2]:
        raise Failure('UNSUPPORTED','Version policy rejects source major/minor; no implicit downgrade or migration')
    # Save/reopen validation is structural. Do not run physics or media to claim usability.
    for collection in (bpy.data.images,bpy.data.movieclips):
        if any(x.source in ('SEQUENCE','TILED') for x in collection):raise Failure('UNSUPPORTED','Project copy sequence/tile expansion requires its domain adapter')
    if bpy.data.cache_files or bpy.data.volumes or any(s.rigidbody_world for s in bpy.data.scenes) or any(o.modifiers for o in bpy.data.objects if any(m.type in ('FLUID','CLOTH','SOFT_BODY') for m in o.modifiers)):
        raise Failure('UNSUPPORTED','Cache-bearing project requires its domain copy adapter')
    paths=file_paths();declared={canonical(d['file']):d for d in params['resources']}
    if set(paths)!=set(declared):raise Failure('INVALID_REQUEST','Copy resources must equal complete native external path inventory')
    for p in paths:
        if not Path(p).is_file() or digest(p)!=declared[p]['expected_sha256']:raise Failure('CONFLICT','Copy resource changed or missing')
    candidate=job/'project-candidate.blend'
    if candidate.exists():raise Failure('CONFLICT','Project candidate already exists')
    bpy.context.preferences.filepaths.save_version=0
    if spec['path_policy']=='MAKE_RELATIVE':
        bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),check_existing=False,relative_remap=True,copy=True,compress=spec['compress'])
    after=inspection.snapshot(str(candidate));after_paths=file_paths()
    changes=inspection.compare(before,after)
    atomic_json(job/'project-before.json',before);atomic_json(job/'project-after.json',after);atomic_json(job/'project-changes.json',changes)
    if not changes['equal_within_scope'] or paths!=after_paths:
        raise Failure('VALIDATION_FAILED','Saved project changed structural content or external path targets; candidate is unaccepted')
    report={'project_report_version':'1.0','operation':'PREPARE_COPY','source':{'file':params['file'],'expected_sha256':params['expected_sha256']},
      'saved_version':list(version),'reader_version':list(reader),'profile':startup_profile(),'version_info':versions,'container':container(candidate),'external_paths':paths,'undeclared_paths':[],
      'datablocks':len(after['datablocks']),'validation':{'open':'pass','render':'not_run','animation':'not_run','dependency_closure':'native_static_paths_only'},
      'source_saved':False,'candidate':str(candidate),'reopen':'pass','path_policy':spec['path_policy'],'version_policy':spec['version_policy']}
    atomic_json(job/'project-report.json',report)
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'report':str(job/'project-report.json'),'reopen':'pass',
      'transaction_item':{'kind':'blend','mode':'create','file':str(candidate),'dependencies':params['resources']},'scope':report['profile']['scope']}
