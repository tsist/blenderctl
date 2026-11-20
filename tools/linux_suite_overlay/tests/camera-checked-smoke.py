"""Opt-in Linux smoke: python3 camera-checked-smoke.py RUNTIME ADDON; factory camera only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

runtime, addon = (Path(v).resolve(strict=True) for v in sys.argv[1:])
cli = Path(__file__).resolve().parents[1] / "bin/camera-checked"
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def must(condition, message):
    if not condition:
        raise RuntimeError(message)

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp).resolve()
    source, request, candidate, plan = (root / n for n in ("source.blend","request.json","candidate","plan.json"))
    expr = ("import bpy; bpy.context.preferences.filepaths.file_preview_type='NONE'; "
            f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r},compress=False)")
    env = {"HOME":temp,"TMPDIR":temp,"PATH":"/usr/bin:/bin","XDG_CONFIG_HOME":temp}
    subprocess.run([runtime/"blender","-b","-noaudio","--factory-startup","-Y","--python-exit-code","9",
                    "--python-expr",expr],env=env,check=True,timeout=90)
    before = digest(source)
    request.write_text('{"scene":"Scene","camera":"Camera","lens":63}')
    def run(*args, success=True):
        p = subprocess.run([cli,runtime,addon,*args],env=env,capture_output=True,text=True,timeout=240)
        if success:
            must(p.returncode == 0, p.stderr)
            return json.loads(p.stdout)
        must(p.returncode != 0 and "CHECK plan hash differs" in p.stderr, p.stderr)
    checked = run("check","--source",source,"--request",request,"--out",candidate,"--plan",plan,"--authorize-owned-work-copy")
    must(not candidate.exists() and checked["plan_sha256"] == digest(plan),"CHECK output mismatch")
    run("apply","--plan",plan,"--plan-sha256","0"*64,success=False)
    must(not candidate.exists(),"bad SHA produced output")
    result = run("apply","--plan",plan,"--plan-sha256",digest(plan))["result"]
    cold = result["cold_readback"]
    must(result["source"]["lens"] == 50 and cold["lens"] == 63,"lens mismatch")
    must(result["source"]["protected"] == cold["protected"],"protected state changed")
    must(result["candidate_sha256"] == digest(candidate/"candidate.blend"),"candidate mismatch")
    must(result["source_sha256"] == before == digest(source),"source changed")
    print("PASS: pinned CHECK/APPLY, wrong SHA rejected, 50→63 mm cold readback, source preserved")
