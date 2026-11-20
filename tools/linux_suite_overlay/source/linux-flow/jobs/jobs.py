#!/usr/bin/python3 -I
"""Local, private, bounded one-shot supervisors. No persistent service or arbitrary commands."""
import argparse, contextlib, ctypes, fcntl, hashlib, json, os, re, signal, stat, subprocess, sys, time, uuid
from pathlib import Path

SELF=Path(__file__).resolve()
BASE=next((p for p in SELF.parents if (p/'af-exterior-linux-suite/bin/blenderctl').is_file()),SELF.parents[2])
CLI=BASE/'af-exterior-linux-suite/bin/blenderctl'
if (CLI.parent/'blenderctl-prepare-v1').is_file():CLI=CLI.parent/'blenderctl-prepare-v1'
RUNTIME=BASE/'af-exterior-blender53/blender-5.3.0-linux-x64/blender'
COMMON=BASE/'af-exterior-linux-suite/jobs/common'
CLI_SHA='c796f6888a24e93caa2a6a72c2e9c7da727af6cf64cb89173f54e59334c8c5ac'
RUN_SHA='baaa36cb5d7a35092388e6d123739b0829012c252b0705bc2debec292e4ebffb'
LIMIT=512*1024*1024
BOOT=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
NS=os.stat('/proc/self/ns/pid').st_ino
LIBC=ctypes.CDLL(None,use_errno=True)
def need(ok,msg):
    if not ok: raise ValueError(msg)
def absolute(value):
    p=Path(value)
    need(p.is_absolute() and '..' not in p.parts and p.resolve(strict=True)==p,'absolute existing non-symlink path required')
    return p
def private(p):
    p=absolute(p); s=p.stat()
    need(stat.S_ISDIR(s.st_mode) and s.st_uid==os.getuid() and stat.S_IMODE(s.st_mode)==0o700,'owned 0700 directory required')
    for parent in p.parents:
        t=parent.stat()
        need(t.st_uid in (0,os.getuid()) and (not t.st_mode & 0o022 or (t.st_uid==0 and t.st_mode & stat.S_ISVTX)), 'unsafe ancestor')
    return p
def checked(p,limit,write=None,capture=False):
    absolute(p); fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(fd)
        need(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<=limit,'invalid size/type/link count')
        h=hashlib.sha256(); buf=[]; n=0
        while chunk:=f.read(1024*1024):
            n+=len(chunk); need(n<=limit,'size limit'); h.update(chunk)
            if write: write.write(chunk)
            if capture: buf.append(chunk)
        t=os.fstat(fd); u=p.lstat()
        sig=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_ctime_ns,v.st_mtime_ns,v.st_nlink)
        need(sig(s)==sig(t)==sig(u) and n==s.st_size,'file changed')
        return h.hexdigest(),b''.join(buf)
def sha(p):return checked(p,LIMIT)[0]
def blob(p):return checked(p,2**20,capture=True)[1]
def obj(p):return json.loads(blob(p))
def syncdir(p):
    fd=os.open(p,os.O_DIRECTORY); os.fsync(fd); os.close(fd)
def put(p,o):
    data=o if isinstance(o,bytes) else json.dumps(o,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()
    tmp=p.with_name('.'+uuid.uuid4().hex)
    with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(tmp,p);syncdir(p.parent)
@contextlib.contextmanager
def lock(p):
    fd=os.open(p,os.O_NOFOLLOW|os.O_NONBLOCK|os.O_RDWR|os.O_CREAT,0o600)
    try:
        s=os.fstat(fd);need(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==os.getuid(),'bad lock')
        fcntl.flock(fd,fcntl.LOCK_EX);yield
    finally:os.close(fd)
def ident(pid):
    try:
        v=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
        if v[0] in ('Z','X'):return None
        return {'pid':pid,'start_ticks':v[19],'boot_id':BOOT,'pid_namespace':NS}
    except (FileNotFoundError,ProcessLookupError):return None
def children(pid):
    try:return list(map(int,Path(f'/proc/{pid}/task/{pid}/children').read_text().split()))
    except FileNotFoundError:
        found=[]
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():continue
            try:
                if int((entry/'stat').read_text().rsplit(')',1)[1].split()[1])==pid:found.append(int(entry.name))
            except (FileNotFoundError,PermissionError,ProcessLookupError):pass
        return found
def alive(w):return bool(w and ident(w['pid'])==w)
def argv(j,r):
    a=[str(CLI),r['domain'],'prepare','--manifest',str(j/'manifest.json')]
    if r['source']:a+=['--file',r['source'],'--expected-sha256',r['source_sha256']]
    return a
def bindings(j,r):
    return {k:r[k] for k in ('job_id','domain','source','source_sha256','manifest_sha256','runtime_sha256')}|{'request_sha256':sha(j/'request.json')}
def validate(j):
    r=obj(j/'request.json')
    allowed={str(CLI.parent/'blenderctl'),str(CLI.parent/'blenderctl-prepare-v1')}
    need(isinstance(r['argv'],list) and r['argv'] and r['argv'][0] in allowed and r['argv'][1:]==argv(j,r)[1:],'request argv mismatch')
    need(type(r['timeout']) is int and 1<=r['timeout']<=300,'invalid request deadline')
    need(r['job_id']==j.name and r['domain'] in ('scene','model') and r['runtime_sha256']==RUN_SHA and r['cli_sha256']==CLI_SHA and sha(j/'manifest.json')==r['manifest_sha256'],'request mismatch')
    return r
def leased(j):
    fd=os.open(j/'lease',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        t=os.fstat(fd);need(stat.S_ISREG(t.st_mode) and t.st_nlink==1 and t.st_uid==os.getuid(),'invalid job lease')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        return False
    finally:os.close(fd)
def fresh(s):
    # Monotonic boot clock is common across caller PID namespaces on this host.
    delta=time.clock_gettime(time.CLOCK_BOOTTIME)-s.get('heartbeat',0)
    return 0<=delta<=30
def state(j):
    r=validate(j);s=obj(j/'status.json');need(s['request_sha256']==sha(j/'request.json'),'status binding mismatch')
    if (j/'result.json').exists() or (j/'result.json').is_symlink():
        z=obj(j/'result.json')
        if s['state'] not in ('running','starting'):need(z==s,'terminal receipts differ')
        need(all(z.get(k)==v for k,v in bindings(j,r).items()),'result binding mismatch')
        if z['state']=='succeeded':
            back=z['backend_job_id'];need(bool(re.fullmatch('[0-9a-f]{32}',back)),'bad backend id')
            underlying=obj(COMMON/back/'result.json'); logged=obj(j/'stdout'); req=obj(COMMON/back/'request.json')
            need(underlying==logged and underlying.get('ok') is True and underlying.get('job_id')==back and underlying['command']==r['domain']+'.prepare' and underlying['data']['candidate_sha256']==z['candidate_sha256'],'backend receipt mismatch')
            need(req['command']==underlying['command'] and req['params'].get('file')==r['source'] and req['params'].get('expected_sha256')==r['source_sha256'] and req['params']['manifest']==obj(j/'manifest.json'),'backend input mismatch')
            need(z['reopen']=='pass' and z['candidate']=='candidate.blend' and sha(j/'candidate.blend')==z['candidate_sha256'],'candidate integrity failure')
        else:need(z['state'] in ('failed','cancelled','timed_out','orphaned'),'invalid terminal state')
        return z
    who=s.get('supervisor')
    if leased(j):
        # Kernel file-description lease survives PID namespace boundaries. No PID
        # presence/absence from another shell can misclassify or signal the worker.
        return s|{'supervisor_observed_here':alive(who),'heartbeat_fresh':fresh(s)}
    else:
        z=bindings(j,r)|{'state':'orphaned','error':'Supervisor missing; not resumed. No candidate accepted.','child':s.get('child'),'supervisor':s.get('supervisor')}
        put(j/'result.json',z);put(j/'status.json',z);return z
def submit(a,root):
    need(1<=a.timeout<=300,'timeout must be 1..300 seconds')
    man=blob(absolute(a.manifest));m=json.loads(man)
    need(isinstance(m,dict) and isinstance(m.get('operations'),list) and 1<=len(m['operations'])<=128,'manifest needs 1..128 operations')
    need(bool(a.source)==bool(a.sha),'source and sha must be paired')
    if a.source:
        p=absolute(a.source);need(p.suffix.lower()=='.blend' and re.fullmatch('[0-9a-f]{64}',a.sha) and sha(p)==a.sha,'invalid source or SHA');a.source=str(p)
    need(sha(CLI)==CLI_SHA and sha(RUNTIME)==RUN_SHA,'runtime/CLI pin mismatch')
    with lock(root/'.submit.lock'):
        entries=[x for x in root.iterdir() if re.fullmatch('[0-9a-f]{32}',x.name)]
        need(len(entries)<256,'root job cap 256; choose another owned private root')
        active=0
        for x in entries:
            private(x)
            if not (x/'result.json').exists() and (x/'status.json').exists() :
                if leased(x):active+=1
        need(active<2,'root already has 2 active jobs')
        j=root/uuid.uuid4().hex;j.mkdir(mode=0o700);syncdir(root)
        with lock(j/'lock'):
            put(j/'manifest.json',man)
            r={'job_id':j.name,'domain':a.domain,'source':a.source,'source_sha256':a.sha,'manifest_sha256':sha(j/'manifest.json'),'runtime_sha256':RUN_SHA,'cli_sha256':CLI_SHA,'timeout':a.timeout}
            r['argv']=argv(j,r);put(j/'request.json',r)
            s=bindings(j,r)|{'state':'starting','heartbeat':time.clock_gettime(time.CLOCK_BOOTTIME),'supervisor':None,'child':None};put(j/'status.json',s)
            lease=os.open(j/'lease',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            try:
                fcntl.flock(lease,fcntl.LOCK_EX)
                w=subprocess.Popen(['/usr/bin/python3','-I','-B',str(SELF),'--root',str(root),'_worker',j.name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,pass_fds=(lease,),env={'PATH':os.defpath,'LANG':'C.UTF-8','AF_JOB_LEASE_FD':str(lease)})
                s['supervisor']=ident(w.pid);need(s['supervisor'],'supervisor disappeared');put(j/'status.json',s)
            finally:os.close(lease)
        return s
def worker(j):
    p=None;child=None;s=None;r=None
    try:
        with lock(j/'lock'):
            fd=int(os.environ['AF_JOB_LEASE_FD'])
            need(os.path.samestat(os.fstat(fd),(j/'lease').stat()) and leased(j),'missing live supervisor lease')
            r=validate(j);s=obj(j/'status.json');need(s['supervisor']==ident(os.getpid()) and s['state']=='starting' and not (j/'result.json').exists(),'unauthorized worker')
            need(s['request_sha256']==sha(j/'request.json') and sha(Path(r['argv'][0]))==CLI_SHA and sha(RUNTIME)==RUN_SHA,'job or executable changed')
            if (j/'cancel').exists():raise ValueError('cancelled before launch')
            parent=os.getpid()
            def on_fork():
                if LIBC.prctl(1,signal.SIGTERM,0,0,0)!=0 or os.getppid()!=parent:os._exit(125)
            with (j/'stdout').open('xb') as out,(j/'stderr').open('xb') as err:
                p=subprocess.Popen(r['argv'],stdin=subprocess.DEVNULL,stdout=out,stderr=err,env={'PATH':os.defpath,'LANG':'C.UTF-8','HOME':str(j)},start_new_session=True,preexec_fn=on_fork)
            child=ident(p.pid);s.update(state='running',child=child);put(j/'status.json',s)
        start=time.monotonic();stop=None;stopped_at=None
        while p.poll() is None:
            now=time.clock_gettime(time.CLOCK_BOOTTIME)
            observed=None
            if not s.get('blender'):
                try:
                    for raw in children(p.pid):
                        k=int(raw)
                        if Path(f'/proc/{k}/cmdline').read_bytes().split(b'\0')[:1]==[os.fsencode(RUNTIME)]:
                            observed=ident(k)
                            if observed:break
                except (FileNotFoundError,ProcessLookupError):pass
            if observed or now-s['heartbeat']>=1:
                with lock(j/'lock'):
                    if (j/'result.json').exists():
                        p.terminate();p.wait();return
                    s['heartbeat']=now
                    if observed:s['blender']=observed
                    put(j/'status.json',s)
            if stop is None:
                if (j/'cancel').exists():stop='cancelled'
                elif time.monotonic()-start>=r['timeout']:stop='timed_out'
                if stop:p.terminate();stopped_at=time.monotonic()
            elif time.monotonic()-stopped_at>8:
                # The un-reaped Popen child cannot be PID-recycled. Observe its actual
                # direct workers and signal via pidfds only after checking parentage.
                try:
                    for rawpid in children(p.pid):
                        kid=int(rawpid)
                        try:
                            fd=os.pidfd_open(kid)
                            try:
                                par=int(Path(f'/proc/{kid}/stat').read_text().rsplit(')',1)[1].split()[1])
                                if par==p.pid:signal.pidfd_send_signal(fd,signal.SIGKILL)
                            finally:os.close(fd)
                        except ProcessLookupError:pass
                finally:p.kill()
            time.sleep(.05)
        z={'state':stop or 'failed'}
        if not stop:
            response=obj(j/'stdout')
            if p.returncode or response.get('ok') is not True:raise ValueError(str(response.get('error') or p.returncode)[:700])
            b=response['job_id'];need(bool(re.fullmatch('[0-9a-f]{32}',b)),'bad backend id')
            work=COMMON/b/'worker';expected=work/(r['domain']+'-candidate.blend')
            data=response['data']
            need(response['command']==r['domain']+'.prepare' and data.get('candidate')==str(expected) and data.get('reopen')=='pass','bad backend receipt')
            if r['source']:need(data.get('source_sha256')==r['source_sha256'] and data.get('source_file')==r['source'] and data.get('source_unchanged') is True and sha(Path(r['source']))==r['source_sha256'],'source changed')
            with (j/'candidate.blend').open('xb') as out:
                h=checked(expected,LIMIT,write=out)[0];out.flush();os.fsync(out.fileno())
            need(h==data.get('candidate_sha256') and h==sha(j/'candidate.blend'),'candidate mismatch')
            z={'state':'succeeded','candidate':'candidate.blend','candidate_sha256':h,'reopen':'pass','backend_job_id':b}
    except Exception as e:
        if p and p.poll() is None:p.terminate();p.wait()
        z={'state':'failed','error':str(e)[:700]}
    if s and r and s.get('supervisor')==ident(os.getpid()):
        with lock(j/'lock'):
            if (j/'result.json').exists():return
            if (j/'cancel').exists() and z['state']!='timed_out':z={'state':'cancelled'}
            need(s['request_sha256']==sha(j/'request.json') and validate(j)==r,'request changed during execution')
            final=bindings(j,r)|z|{'child':child,'blender':s.get('blender'),'supervisor':s['supervisor']}
            put(j/'result.json',final);put(j/'status.json',final)
def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);sub=ap.add_subparsers(dest='op',required=True)
    q=sub.add_parser('submit');q.add_argument('--domain',choices=['scene','model'],required=True);q.add_argument('--manifest',required=True);q.add_argument('--source');q.add_argument('--sha');q.add_argument('--timeout',type=int,default=120)
    for name in ('status','result','cancel','_worker'):sub.add_parser(name).add_argument('id')
    a=ap.parse_args();root=private(Path(a.root))
    if a.op=='submit':z=submit(a,root)
    else:
        need(bool(re.fullmatch('[0-9a-f]{32}',a.id)),'invalid job id');j=private(root/a.id)
        if a.op=='_worker':worker(j);return 0
        with lock(j/'lock'):
            z=state(j)
            if a.op=='result':need(z['state'] not in ('running','starting','unobservable'),'result not ready')
            if a.op=='cancel' and z['state'] in ('running','starting','unobservable'):
                put(j/'cancel',b'cancel');z=z|{'cancel_requested':True}
    print(json.dumps(z,ensure_ascii=False));return 0
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print(json.dumps({'error':str(exc)[:700]}));sys.exit(2)
