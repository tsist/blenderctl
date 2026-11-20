"""Compatibility dispatch from the old isolated Linux blenderctl launcher."""
import json, os, sys
from pathlib import Path

def stop(msg, code="UNSUPPORTED", rc=10):
    print(json.dumps({"schema_version":"1.0","cli_version":"0.63.1","command":None,
                      "job_id":None,"ok":False,"data":None,
                      "error":{"code":code,"message":msg},"artifacts":{}}))
    raise SystemExit(rc)

def main():
    workspace=Path(__file__).resolve().parents[3]
    suite=workspace/"af-exterior-linux-suite"
    old=workspace/"af-exterior-blender53"
    official=old/"blender-5.3.0-linux-x64"/"blender"
    argv=sys.argv[1:]
    options=[]
    while argv and argv[0].startswith("--"):
        x=argv[0]
        if x in {"--version","--help"}:
            if len(argv)!=1: stop("Use "+x+" on its own","INVALID_REQUEST",2)
            os.execv(suite/"bin/blenderctl", ["blenderctl",x])
        if x in {"--compact","--full","--human"}:
            options.append(argv.pop(0)); continue
        if x in {"--timeout","--jobs-dir"}:
            if len(argv)<2:stop(x+" needs a value","INVALID_REQUEST",2)
            options.extend(argv[:2]); argv=argv[2:]; continue
        if any(x.startswith(y+"=") for y in {"--timeout","--jobs-dir"}):
            options.append(argv.pop(0)); continue
        stop("The legacy Linux launcher does not support this global option: "+x)
    if not argv or argv[0]=="-h":os.execv(suite/"bin/blenderctl",["blenderctl","--help"])
    verb=argv[0]; sub=argv[1] if len(argv)>1 else ""
    py="/usr/bin/python3.13"
    default=options+[str(x) for x in argv]
    if verb in {"doctor","capabilities"}:
        # Read-only stock diagnostics. No stock saved-file transaction fallback.
        target=old/"repos/blenderctl/tools/blenderctl/cli.py"
        args=[py,"-B",str(target),"--blender",str(official),"--jobs-dir",str(old/"jobs"),*default]
    elif verb in {"inspect","query"} or (verb in {"scene","model","exchange"} and (sub in {"--help","-h"} or (sub in {"inspect","prepare"} if verb in {"scene","model"} else sub=="export"))):
        target=workspace/"af-exterior-linux-dev/source/blenderctl/scripts/blenderctl_linux.py"
        args=[py,"-B",str(target),"--blender",str(official),"--jobs-dir",str(suite/"jobs/common"),*default]
    elif verb in {"geometry","lighting"} and sub in {"run","inspect","--help","-h"}:
        target=suite/"source/geo-light/scripts/blenderctl_linux.py"
        args=[py,"-B",str(target),"--blender",str(official),"--jobs-dir",str(suite/"jobs/geo-light"),*default]
    elif verb in {"job","transaction","camera-advanced","camera-shots","camera-edit","camera-framing","compositing","modeling","reference-contour","workbench"}:
        if options:stop("Place the operation's own options after its name","INVALID_REQUEST",2)
        target=suite/"bin/blenderctl"
        args=[str(target),*argv]
    else:
        stop("No local non-render Linux implementation for: "+" ".join(argv[:2]))
    os.execv(args[0],args)

if __name__=="__main__":main()
