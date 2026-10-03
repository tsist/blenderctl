# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only cache plans and verified local deployment; never clean automatically."""
import os,re,time,shutil,subprocess,sys
from pathlib import Path
from protocol import ROOT,VERSION,DEFAULT_BLENDER,Failure,read_json,atomic_json,digest
from filesystem import FileGuard,native_path
from installation import content_inventory,runtime_files

def safe_root(path):
    p=Path(path)
    if any(x.is_symlink() or x.is_junction() for x in [p,*p.parents]):raise Failure('UNSUPPORTED','Managed root cannot contain links')
    if not p.is_dir():raise Failure('NOT_FOUND','Managed root not found')
    return p.resolve()

def cache_plan(spec,checkpoint):
    root=safe_root(spec['jobs_root']);jobs={};references=set(spec['references']);unknown=[]
    for p in root.iterdir():
        checkpoint()
        if not re.fullmatch('[0-9a-f]{32}',p.name) or not p.is_dir():continue
        if p.is_symlink() or p.is_junction():unknown.append(str(p));continue
        try:
            state=read_json(p/'status.json');req=read_json(p/'request.json')
            if state.get('job_id')!=p.name:raise ValueError('identity')
            jobs[p.name]={'path':p,'state':state,'request':req,'reports':[]}
            for f in (p/'pipeline-report.json',p/'pipeline-state.json'):
                if f.is_file():jobs[p.name]['reports'].append(read_json(f))
        except (Failure,OSError,ValueError):unknown.append(str(p))
    def referenced(v,owner):
        if isinstance(v,dict):
            for x in v.values():referenced(x,owner)
        elif isinstance(v,list):
            for x in v:referenced(x,owner)
        elif isinstance(v,str):
            if v in jobs and v!=owner:references.add(v)
            try:
                p=Path(v)
                if p.is_absolute() and p.resolve().is_relative_to(root):
                    ident=p.resolve().relative_to(root).parts[0]
                    if ident in jobs and ident!=owner:references.add(ident)
            except (OSError,ValueError,IndexError):pass
    for ident,j in jobs.items():referenced([j['request'],j['reports']],ident)
    rows=[]
    for ident,j in jobs.items():
        checkpoint();state=j['state'];reasons=[];hashes={};changed=[]
        if state.get('state') not in ('succeeded','failed','cancelled','timed_out'):reasons.append('active_or_unknown')
        if ident in spec['pins']:reasons.append('pinned')
        if ident in references:reasons.append('referenced')
        if time.time()-state.get('finished_at',time.time())<spec['keep_days']*86400:reasons.append('retention_period')
        for r in j['reports']:
            for step in r.get('steps',[]):hashes.update(step.get('output_hashes',{}))
        for file,expected in hashes.items():
            checkpoint()
            try:
                with FileGuard(file) as g:actual=g.sha256(checkpoint)
                if actual!=expected:changed.append(file)
            except (Failure,OSError):changed.append(file)
        if not hashes:reasons.append('no_reusable_content_manifest')
        if changed:reasons.append('content_changed_or_missing')
        rows.append({'job_id':ident,'path':str(j['path']),'state':state.get('state'),'content_files':len(hashes),'changed':changed,'keep_reasons':reasons,'eligible_for_later_review':not reasons})
    # Unknown managed entries may carry references that cannot be established.
    if unknown:
        for row in rows:row['keep_reasons'].append('unknown_entries_may_reference');row['eligible_for_later_review']=False
    return {'version':'1.0','root':str(root),'rows':rows,'unknown':unknown,'dry_run':True,'deleted':[],'scope':'Plan only; pins/references explicit plus observed cross-job references. No deletion authority or complete external-reference claim.'}

def package(spec,blender,job,checkpoint):
    out=Path(native_path(job/'package'));out.mkdir();files=[*sorted((ROOT/'tools/blenderctl').glob('*.py')),ROOT/'blenderctl.ps1']
    files += [p for p in (ROOT/'docs/cli').rglob('*') if p.is_file()]
    mapping=[(p,p.relative_to(ROOT)) for p in files]
    runtime=content_inventory(blender,checkpoint);runtime_root=Path(native_path(Path(blender).resolve().parent))
    if spec['include_runtime']:
        for p in runtime_files(blender):mapping.append((p,DEFAULT_BLENDER.relative_to(ROOT).parent/p.relative_to(runtime_root)))
    total=sum(p.stat().st_size for p,_ in mapping)
    if total>spec['max_bytes']:raise Failure('RESOURCE_LIMIT','Package exceeds declared byte budget')
    entries=[]
    for p,relative in mapping:
        checkpoint();target=out/relative;target.parent.mkdir(parents=True,exist_ok=True)
        with FileGuard(p) as g:
            sha=g.sha256(checkpoint);shutil.copyfile(p,target)
            if digest(target,checkpoint)!=sha:raise Failure('CONFLICT','Package copy hash differs')
        entries.append({'path':relative.as_posix(),'sha256':sha,'bytes':target.stat().st_size})
    report={'version':'1.0','cli_version':VERSION,'files':entries,'runtime':{'included':spec['include_runtime'],'blender_relative':DEFAULT_BLENDER.relative_to(ROOT).as_posix(),'files':runtime},'excluded':['portable user configuration and extensions','assets','audit and cache history'],'requirements':['Windows local disk','PowerShell 7 for wrapper','matching Blender runtime; explicit --blender when external'],'environment_validation':{'local_relocation':'not_run','clean_windows':'not_run','other_os':'not_run','network_storage':'not_run','power_loss':'not_run'}}
    atomic_json(out/'package-manifest.json',report)
    return {'directory':str(job/'package'),'manifest':str(job/'package/package-manifest.json'),'manifest_sha256':digest(out/'package-manifest.json'),'bytes':total,'files':len(entries),'runtime_included':spec['include_runtime']}

def verify_install(path,blender,checkpoint):
    root=safe_root(path);manifest=read_json(root/'package-manifest.json');problems=[]
    if manifest.get('version')!='1.0':raise Failure('INVALID_REQUEST','Unknown package manifest')
    for row in manifest['files']:
        checkpoint();relative=Path(row['path']);p=root/relative
        if relative.is_absolute() or '..' in relative.parts or not p.resolve().is_relative_to(root):raise Failure('INVALID_REQUEST','Package manifest path escapes root')
        try:
            with FileGuard(p) as g:sha=g.sha256(checkpoint)
            if sha!=row['sha256']:problems.append({'file':str(p),'reason':'hash_changed'})
        except (Failure,OSError):problems.append({'file':str(p),'reason':'missing_or_unreadable'})
    relative=Path(manifest['runtime']['blender_relative'])
    if relative.is_absolute() or '..' in relative.parts:raise Failure('INVALID_REQUEST','Runtime path escapes package')
    engine=root/relative if manifest['runtime']['included'] else Path(blender)
    if not engine.is_file():problems.append({'file':str(engine),'reason':'runtime_missing'})
    else:
        observed=content_inventory(engine,checkpoint)
        if observed!=manifest['runtime']['files']:problems.append({'file':str(engine.parent),'reason':'runtime_content_inventory_differs'})
    report={'version':'1.0','directory':str(root),'verified':not problems,'problems':problems,'engine':str(engine),'scope':'File integrity only; independent launch and clean-machine checks are separate'}
    return report

def run(request,launch,job,checkpoint):
    command=request['command'];p=request['params']
    if command.startswith('resource.'):
        import resource_leases
        result=resource_leases.init(p['domain'],p['manifest'],checkpoint) if command=='resource.init' else resource_leases.status(p['domain'],checkpoint)
    elif command.startswith('cache.'):result=cache_plan(p['manifest'],checkpoint)
    elif command=='project.package':result=package(p['manifest'],launch['blender'],job,checkpoint)
    elif command=='doctor.verify_install':result=verify_install(p['directory'],launch['blender'],checkpoint)
    else:raise Failure('INVALID_REQUEST','Unknown host operation')
    atomic_json(job/'operation-report.json',result)
    if result.get('verified') is False:raise Failure('VALIDATION_FAILED','Install verification failed; see operation-report.json')
    return result
