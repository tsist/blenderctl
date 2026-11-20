"""Linux-only, new-only publication of independently reopened job candidates."""
import argparse, contextlib, fcntl, hashlib, json, os, re, secrets, shutil, stat, subprocess, sys, tempfile
from pathlib import Path
from contextlib import ExitStack
D=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
R=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
def need(ok,msg):
    if not ok: raise ValueError(msg)
def directory(path,private=False):
    p=Path(path)
    need(p.is_absolute() and '..' not in p.parts,'absolute path without .. required')
    fd=os.open('/',D)
    try:
        for k in p.parts[1:]:
            n=os.open(k,D,dir_fd=fd);os.close(fd);fd=n
        if private:
            for parent in p.parents:
                ps=parent.stat();need(ps.st_uid in (0,os.geteuid()) and (not ps.st_mode & 0o022 or (ps.st_uid==0 and ps.st_mode & stat.S_ISVTX)),'unsafe directory ancestor')
            s=os.fstat(fd);need(s.st_uid==os.geteuid() and stat.S_IMODE(s.st_mode)==0o700,'directory must be private and owned (0700)')
        return fd
    except BaseException: os.close(fd);raise
def opened(stack,fd): stack.callback(os.close,fd);return fd
def readfd(stack,dr,name,private=True):
    fd=opened(stack,os.open(name,R,dir_fd=dr));s=os.fstat(fd)
    need(stat.S_ISREG(s.st_mode) and (not private or (s.st_nlink==1 and s.st_uid==os.geteuid() and s.st_mode & 0o022==0)),'unexpected file ownership, type, links or mode')
    return fd
def digest(fd):
    h=hashlib.sha256()
    with os.fdopen(os.dup(fd),'rb') as f:
        f.seek(0)
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()
def raw(fd):
    os.lseek(fd,0,0);data=os.read(fd,2097153);need(len(data)<=2097152,'oversized receipt');return data
def identity(s):return [s.st_dev,s.st_ino]
def entry(d,n):
    try:return os.stat(n,dir_fd=d,follow_symlinks=False)
    except FileNotFoundError:return None
def verify(binary,dr,name):
    expr="""import bpy
assert bpy.data.filepath and bpy.context.scene and bpy.data.scenes
paths={p for p in bpy.utils.blend_paths(absolute=False,packed=False) if p}
# Blender 5.3 serializes the factory grease-pencil brush chooser even in an
# empty scene. It is not a library or a required datablock dependency.
refs={p for vals in bpy.data.file_path_map(include_libraries=True).values() for p in vals}
assert not bpy.data.libraries
for p in paths:
    assert p not in refs and p.endswith('/datafiles/assets/brushes/essentials_brushes-gp_draw.blend'), 'external dependency: '+p
print('AF_PUBLISH_REOPEN_OK:'+bpy.app.version_string)"""
    with tempfile.TemporaryDirectory(prefix='af-reopen-') as home:
        env={'PATH':'/usr/bin:/bin','HOME':home,'XDG_CONFIG_HOME':home,'XDG_CACHE_HOME':home,'TMPDIR':home,'LANG':'C.UTF-8'}
        p=subprocess.run([binary,'--background','--factory-startup','--disable-autoexec','--offline-mode','-t','1',f'/proc/self/fd/{dr}/{name}','--python-exit-code','41','--python-expr',expr],env=env,pass_fds=(dr,),capture_output=True,text=True,errors='replace',timeout=120)
        markers=[l for l in p.stdout.splitlines() if l.startswith('AF_PUBLISH_REOPEN_OK:')]
        need(p.returncode==0 and len(markers)==1,'independent Blender reopen/dependency check failed: '+(p.stdout+p.stderr)[-800:])
        return markers[0].split(':',1)[1]
def integration():
    import importlib.util
    f=Path(__file__).resolve().parent.parent/'jobs/jobs.py'
    spec=importlib.util.spec_from_file_location('af_publication_job_contract',f)
    jobs=importlib.util.module_from_spec(spec);spec.loader.exec_module(jobs)
    return jobs
def backend_confirm(root,job,res):
    # A terminal result is required before calling state: no orphan transition.
    jobs=integration();jobs.private(root);jobs.private(root/job)
    need(jobs.state(root/job)==res,'backend attestation mismatch')
def binary_for(requested,sha):
    jobs=integration()
    path=jobs.absolute(requested or jobs.RUNTIME)
    need(sha==jobs.RUN_SHA and jobs.sha(path)==sha,'runtime SHA mismatch')
    return str(path)
def transact(a):
    need(re.fullmatch('[a-f0-9]{32}',a.job_id),'invalid job id')
    out=Path(a.output);need(out.is_absolute() and not out.name.startswith('.') and out.suffix=='.blend','output must be an absolute non-hidden .blend')
    root=Path(a.jobs_root);need(root not in out.parents and root!=out.parent,'publication must be outside job store')
    with ExitStack() as st:
        Dopen=lambda p,private:opened(st,directory(p,private))
        od=Dopen(out.parent,True);rootfd=Dopen(root,True)
        jd=opened(st,os.open(a.job_id,D,dir_fd=rootfd));sj=os.fstat(jd);need(sj.st_uid==os.geteuid() and stat.S_IMODE(sj.st_mode)==0o700,'unsafe job directory')
        fcntl.flock(od,fcntl.LOCK_EX if a.action=='publish' else fcntl.LOCK_SH)
        reqraw=raw(readfd(st,jd,'request.json'));req=json.loads(reqraw);res=json.loads(raw(readfd(st,jd,'result.json')))
        backend_confirm(root,a.job_id,res)
        h=lambda b:hashlib.sha256(b).hexdigest()
        need(res['state']=='succeeded' and res['reopen']=='pass' and res['candidate']=='candidate.blend' and res['job_id']==req['job_id']==a.job_id,'no successful candidate receipt')
        need(h(reqraw)==res['request_sha256'] and digest(readfd(st,jd,'manifest.json'))==res['manifest_sha256'],'request or manifest digest mismatch')
        for key in ('source','source_sha256','domain','manifest_sha256','runtime_sha256'):need(req[key]==res[key],key+' mismatch')
        sf=None
        if res['source'] is not None:
            need(a.source==res['source'],'caller must explicitly identify original source')
            sp=Path(a.source);sd=Dopen(sp.parent,False);sf=readfd(st,sd,sp.name,False)
            need(digest(sf)==res['source_sha256'],'source changed')
        else:need(a.source is None and res['source_sha256'] is None,'unexpected source')
        cf=readfd(st,jd,'candidate.blend');ch=digest(cf);need(ch==res['candidate_sha256'],'candidate changed')
        key=h(out.name.encode())[:24];txn='.af-publish-'+key
        present=entry(od,txn)
        anchor_identity=identity(os.fstat(od))
        if not present:
            if a.action=='status':return {'state':'collision' if entry(od,out.name) else 'not_started'}
            need(not entry(od,out.name),'output already exists')
            os.mkdir(txn,0o700,dir_fd=od);os.fsync(od)
        td=opened(st,os.open(txn,D,dir_fd=od));ts=os.fstat(td)
        need(ts.st_uid==os.geteuid() and stat.S_IMODE(ts.st_mode)==0o700,'unsafe transaction directory')
        data={'job_id':a.job_id,'request_sha256':res['request_sha256'],'source_sha256':res['source_sha256'],'candidate_sha256':ch,'output':str(out),'directory_identity':identity(os.fstat(od))}
        note='prepared.json'
        if entry(td,note):
            rec=json.loads(raw(readfd(st,td,note)));need(all(rec.get(k)==v for k,v in data.items()),'transaction ownership mismatch')
        else:
            need(not entry(od,out.name),'unrelated output collision')
            if a.action=='status':return {'state':'unprepared',**data}
            # Never blindly unlink remnants: interrupted pre-prepare attempts can
            # be inspected. Cap remnants so invalid inputs cannot fill a volume.
            need(len(os.listdir(td))<4,'too many interrupted preparations; inspect the private transaction directory')
            name='stage-'+secrets.token_hex(16)+'.blend'
            fd=opened(st,os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,dir_fd=td))
            with os.fdopen(os.dup(cf),'rb') as src,os.fdopen(os.dup(fd),'wb') as dst:
                src.seek(0);shutil.copyfileobj(src,dst,1048576)
            os.fsync(fd);need(digest(fd)==ch,'stage copy mismatch')
            try:
                v=verify(binary_for(a.blender,res['runtime_sha256']),td,name);need(digest(fd)==ch,'stage changed during reopen')
            except Exception:
                staged_now=entry(td,name)
                if staged_now and identity(staged_now)==identity(os.fstat(fd)):os.unlink(name,dir_fd=td);os.fsync(td)
                raise
            rec={**data,'stage':name,'stage_identity':identity(os.fstat(fd)),'blender_version':v}
            temp='receipt-'+secrets.token_hex(16)
            with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=td),'w') as f:
                json.dump(rec,f);f.flush();os.fsync(f.fileno())
            os.rename(temp,note,src_dir_fd=td,dst_dir_fd=td);os.fsync(td)
        name=rec['stage'];need(re.fullmatch('stage-[0-9a-f]{32}\\.blend',name),'invalid stage name')
        staged=entry(td,name);dst=entry(od,out.name)
        if dst:
            need(stat.S_ISREG(dst.st_mode) and identity(dst)==rec['stage_identity'],'unrelated output collision')
            need(digest(readfd(st,od,out.name,False))==ch,'published file changed')
        else:
            need(staged and identity(staged)==rec['stage_identity'] and digest(readfd(st,td,name))==ch,'stage lost or changed')
            if a.action=='status':return {'state':'prepared',**data}
            need(identity(os.fstat(Dopen(out.parent,True)))==data['directory_identity'],'output directory retargeted')
            if sf is not None:
                now=readfd(st,sd,sp.name,False);need(identity(os.fstat(now))==identity(os.fstat(sf)) and digest(now)==res['source_sha256'],'source changed before commit')
            os.link(name,out.name,src_dir_fd=td,dst_dir_fd=od,follow_symlinks=False);os.fsync(od)
        if a.action=='publish' and staged:
            need(identity(staged)==rec['stage_identity'],'stage identity changed')
            os.unlink(name,dir_fd=td);os.fsync(td)
        need(identity(os.fstat(Dopen(out.parent,True)))==anchor_identity,'output directory moved during transaction; inspect original directory')
        return {'state':'published','blender_version':rec['blender_version'],**data}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['publish','status']);p.add_argument('--jobs-root','--root',required=True);p.add_argument('--job-id',required=True);p.add_argument('--source');p.add_argument('--output',required=True);p.add_argument('--blender',default=None);a=p.parse_args()
    try:print(json.dumps(transact(a),sort_keys=True));return 0
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as e:print(json.dumps({'state':'refused','reason':str(e)}),file=sys.stderr);return 2
if __name__=='__main__':sys.exit(main())
