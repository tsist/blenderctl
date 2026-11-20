"""CHECK or APPLY a bounded native Linux camera operation without opening the panel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

def require(ok, message):
    if not ok:
        raise ValueError(message)

def path(value):
    p = Path(value)
    require(p.is_absolute() and p.resolve() == p, "Use an absolute path without symlinks or parent traversal")
    return p

def owned_directory(p):
    s = p.stat()
    require(stat.S_ISDIR(s.st_mode) and s.st_uid == os.geteuid() and not s.st_mode & 0o077,
            "CHECK and candidate locations require an owner-only directory (0700)")

def read_file(p, cap, *, private=False):
    with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        before = os.fstat(handle.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= cap,
                "Expected a single regular file within its size limit")
        if private:
            require(before.st_uid == os.geteuid() and not before.st_mode & 0o077, "The CHECK file must be owner-only")
        b = handle.read(cap + 1)
        after = os.fstat(handle.fileno())
        sig = lambda q: (q.st_dev, q.st_ino, q.st_size, q.st_mtime_ns, q.st_ctime_ns)
        require(sig(before) == sig(after) == sig(p.lstat()) and len(b) == before.st_size, "File changed while being read")
    return b

def json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()

def decode(b):
    def keys(pairs):
        d = {}
        for key, value in pairs:
            require(key not in d, "JSON contains duplicate keys")
            d[key] = value
        return d
    def invalid(x):
        raise ValueError("Nonfinite JSON number: " + x)
    return json.loads(b, object_pairs_hook=keys, parse_constant=invalid)

def sha(b):
    return hashlib.sha256(b).hexdigest()

def package_identity(addon):
    actual = decode(read_file(addon/"CODE-MANIFEST.json", 256*1024))["files"]
    inv = {p.relative_to(addon).as_posix() for p in addon.rglob("*.py") if "__pycache__" not in p.parts}
    require(set(actual) == inv, "Camera package inventory changed")
    for n, h in actual.items():
        require(sha(read_file(path(addon/n), 4*1024*1024)) == h, "Camera package identity mismatch: "+n)
    return sha(json_bytes(actual))

def invoke(args, source, original_sha, out, query, authorize, *, check):
    with tempfile.TemporaryDirectory(prefix=".camera-cli-", dir=out.parent) as temp:
        req = Path(temp)/"request.json"
        req.write_bytes(json_bytes(query))
        runtime = args.runtime
        command = [str(runtime/"5.3/python/bin/python3.13"), "-I", "-B",
                   str(args.addon/"linux_static/transaction.py"), "--runtime", str(runtime),
                   "--source", str(source), "--sha256", original_sha,
                   "--query", str(req), "--out", str(out)]
        if authorize:
            command.append("--authorize-owned-static-workcopy")
        if check:
            command.append("--check-only")
        proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=170, text=True,
                              env={"HOME":temp, "TMPDIR":temp, "PATH":"/usr/bin:/bin"})
        if proc.returncode:
            lines = [s.strip() for s in proc.stderr.splitlines() if s.strip().startswith(("ValueError:", "RuntimeError:", "PermissionError:"))]
            message = lines[-1] if lines else proc.stderr.strip()[-1200:]
            raise ValueError(message or "Camera transaction failed")
        if check:
            value = decode(proc.stdout.encode())
            require(value["source_sha256"] == original_sha and value["query"] == query and not out.exists(),
                    "CHECK receipt does not match its requested source or destination")
            return value
        value = decode(read_file(out/"receipt.json", 1024*1024, private=True))
        require(value["source_sha256"] == original_sha and value["query"] == query, "APPLY receipt mismatch")
        require(sha(read_file(out/"candidate.blend", 32*1024*1024, private=True)) == value["candidate_sha256"], "Candidate hash differs")
        return value

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime", required=True, type=path, help=argparse.SUPPRESS)
    p.add_argument("--addon", required=True, type=path, help=argparse.SUPPRESS)
    sub=p.add_subparsers(dest="cmd", required=True)
    check=sub.add_parser("check", help="Write a CHECK receipt, not a scene")
    for n in ("source","request","out","plan"):
        check.add_argument("--"+n,required=True,type=path)
    check.add_argument("--authorize-owned-work-copy",action="store_true")
    apply=sub.add_parser("apply",help="Apply an unchanged, explicitly reviewed CHECK")
    apply.add_argument("--plan",required=True,type=path)
    apply.add_argument("--plan-sha256",required=True)
    a=p.parse_args()
    try:
        pkg=package_identity(a.addon)
        owned_directory(a.plan.parent)
        if a.cmd=="check":
            require(not a.plan.exists(), "CHECK plan already exists")
            owned_directory(a.out.parent)
            original_sha=sha(read_file(a.source,32*1024*1024))
            query=decode(read_file(a.request,32*1024))
            checked=invoke(a,a.source,original_sha,a.out,query,a.authorize_owned_work_copy,check=True)
            plan={"version":1,"package":pkg,"source":str(a.source),"out":str(a.out),
                  "authorize":a.authorize_owned_work_copy,"check":checked}
            blob=json_bytes(plan)
            with os.fdopen(os.open(a.plan,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),"wb") as f:
                f.write(blob);f.flush();os.fsync(f.fileno())
            print(json_bytes({"status":"checked","plan":str(a.plan),"plan_sha256":sha(blob),"check":checked}).decode(),end="")
        else:
            blob=read_file(a.plan,1024*1024,private=True)
            require(sha(blob)==a.plan_sha256,"CHECK plan hash differs")
            plan=decode(blob)
            require(plan["version"]==1 and plan["package"]==pkg, "Camera package changed since CHECK; run CHECK again")
            source,out=map(path,(plan["source"],plan["out"]))
            owned_directory(out.parent)
            checked=plan["check"]
            original_sha=checked["source_sha256"]
            require(sha(read_file(source,32*1024*1024))==original_sha,"Source changed since CHECK")
            fresh=invoke(a,source,original_sha,out,checked["query"],plan["authorize"],check=True)
            require(fresh==checked, "Fresh CHECK differs; create a new plan")
            result=invoke(a,source,original_sha,out,checked["query"],plan["authorize"],check=False)
            require(result["source"]==checked["baseline"] and result.get("prediction")==checked.get("prediction"),
                    "Candidate source/prediction differs from CHECK")
            print(json_bytes({"status":"applied","receipt":str(out/"receipt.json"),"result":result}).decode(),end="")
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as e:
        p.exit(2,str(e)+"\n")

if __name__=="__main__":
    main()
