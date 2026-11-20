"""Read only the native markers and pinned legacy Camera Live rows from a trusted work-copy."""
import argparse, hashlib, importlib.util, json, os, stat, subprocess, sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
from camera_advanced_cli import require, path, read_file, package_identity

def sha(b):
    return hashlib.sha256(b).hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--addon",required=True,type=path,help=argparse.SUPPRESS)
    p.add_argument("--runtime",required=True,type=path,help=argparse.SUPPRESS)
    p.add_argument("--source",required=True,type=path)
    p.add_argument("--frame",required=True,type=int)
    p.add_argument("--scene")
    a=p.parse_args()
    try:
        require(-1048574<=a.frame<=1048574,"Frame out of range")
        require(a.scene is None or 1<=len(a.scene)<=128,"Invalid scene")
        identity=package_identity(a.addon)
        py_file=a.addon/"linux_static/transaction.py"
        impl=importlib.util.spec_from_file_location("camera_local_policy",py_file)
        policy=importlib.util.module_from_spec(impl);impl.loader.exec_module(policy)
        for n,h in [("blender",policy.SHA_B),("5.3/python/bin/python3.13",policy.SHA_P)]:
            b=a.runtime/n
            with b.open("rb") as f: require(hashlib.file_digest(f,"sha256").hexdigest()==h,"Runtime fingerprint differs")
        s=a.source.parent.stat()
        require(s.st_uid==os.geteuid() and not s.st_mode&0o077,"Use an owner-only work-copy directory")
        st=a.source.lstat(); require(st.st_uid==os.geteuid() and not st.st_mode&0o022 and a.source.suffix.lower()==".blend","Use an owned, non-writable-by-others .blend")
        raw=read_file(a.source,16*1024*1024)
        require(raw.startswith(b"BLENDER"),"Save the work-copy uncompressed")
        sign=lambda q:(q.st_dev,q.st_ino,q.st_size,q.st_mtime_ns,q.st_ctime_ns)
        check_query=json.dumps({"scene":a.scene,"frame":a.frame}).encode()
        bwrap=["/usr/bin/bwrap","--unshare-pid","--unshare-ipc","--unshare-uts","--die-with-parent","--new-session",
               "--clearenv","--ro-bind","/usr","/usr","--symlink","usr/lib","/lib","--symlink","usr/lib64","/lib64",
               "--symlink","usr/bin","/bin","--ro-bind","/etc/ld.so.cache","/etc/ld.so.cache","--ro-bind","/etc/fonts","/etc/fonts",
               "--proc","/proc","--dev","/dev","--tmpfs","/tmp","--dir","/home","--dir","/trusted","--setenv","HOME","/tmp",
               "--setenv","TMPDIR","/tmp","--setenv","PATH","/usr/bin","--setenv","PYTHONDONTWRITEBYTECODE","1",
               "--setenv","OMP_NUM_THREADS","2","--ro-bind",str(a.runtime),"/blender"]
        descriptors=[]
        try:
            filter_fd=policy.nonet();descriptors.append(filter_fd)
            files=[(raw,"/input.blend"),(check_query,"/query.json")]
            files += [((HERE/n).read_bytes(),"/trusted/"+n) for n in ("pinned_schema.py","shot_table.py","shot_worker.py")]
            for b,n in files:
                fd=policy.fd(b);descriptors.append(fd);bwrap += ["--ro-bind-data",str(fd),n]
            bwrap+=["--seccomp",str(filter_fd),"--","/blender/blender","--factory-startup","--disable-autoexec","--offline-mode",
                    "--background","-t","2","--python-exit-code","8","--python","/trusted/shot_worker.py"]
            r=subprocess.run(bwrap,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=45,pass_fds=descriptors)
        finally:
            for x in descriptors:os.close(x)
        if r.returncode:
            error=[x for x in r.stdout.decode(errors="replace").splitlines() if x.startswith(("ValueError:","AssertionError:","RuntimeError:"))]
            raise ValueError(error[0] if error else "Isolated Blender inspection failed")
        matches=[row.removeprefix(b"SHOT-INSPECT:") for row in r.stdout.splitlines() if row.startswith(b"SHOT-INSPECT:")]
        require(len(matches)==1,"Expected exactly one shot-table receipt")
        require(sign(st)==sign(a.source.lstat()) and sha(read_file(a.source,16*1024*1024))==sha(raw),"Input changed during read")
        d=json.loads(matches[0]);d["source_sha256"]=sha(raw);d["registered_package"]=identity
        print(json.dumps(d,ensure_ascii=False,indent=2))
    except (ValueError,KeyError,OSError,AssertionError,subprocess.TimeoutExpired) as e:
        p.exit(2,str(e)+"\n")

if __name__=="__main__":
    main()
