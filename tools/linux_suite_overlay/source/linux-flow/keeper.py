"""Keep an execution session alive while a bounded Linux local job runs."""
import argparse,json,subprocess,sys,time
from pathlib import Path
SCRIPT=Path(__file__).resolve().parent/"jobs/jobs.py"
def call(root,*args):
    p=subprocess.run([sys.executable,"-I","-B",str(SCRIPT),"--root",root,*args],capture_output=True,text=True,timeout=20)
    if p.returncode:
        raise RuntimeError((p.stdout+p.stderr).strip()[-700:] or "Local job command failed")
    return json.loads(p.stdout)
def require(v,m):
    if not v: raise ValueError(m)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",required=True)
    ss=p.add_subparsers(dest="op",required=True)
    a=ss.add_parser("wait",help="Wait for a job terminal state within this running session")
    a.add_argument("job_id")
    a.add_argument("--wait-seconds",type=int,default=300)
    a=ss.add_parser("submit-and-wait",help="Submit and hold this execution session until a terminal state")
    a.add_argument("--domain",required=True,choices=("scene","model"))
    a.add_argument("--manifest",required=True)
    a.add_argument("--source");a.add_argument("--sha")
    a.add_argument("--timeout",type=int,default=120)
    ns=p.parse_args()
    try:
        if ns.op=="submit-and-wait":
            require(bool(ns.source)==bool(ns.sha),"Source and SHA must be supplied together")
            args=["submit","--domain",ns.domain,"--manifest",ns.manifest,"--timeout",str(ns.timeout)]
            if ns.source:args.extend(("--source",ns.source,"--sha",ns.sha))
            first=call(ns.root,*args)
            print(json.dumps(first,ensure_ascii=False),flush=True)
            job=first["job_id"]; seconds=ns.timeout+20
        else:job=ns.job_id;seconds=ns.wait_seconds
        require(1<=seconds<=320,"Wait supports 1–320 seconds")
        deadline=time.monotonic()+seconds
        while True:
            state=call(ns.root,"status",job)
            if state["state"] in {"succeeded","failed","timed_out","cancelled","orphaned"}:
                print(json.dumps(state,ensure_ascii=False),flush=True)
                return 0 if state["state"]=="succeeded" else 1
            if time.monotonic()>=deadline:
                print(json.dumps(state,ensure_ascii=False),flush=True)
                return 4
            time.sleep(.4)
    except (ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
if __name__=="__main__":sys.exit(main())
