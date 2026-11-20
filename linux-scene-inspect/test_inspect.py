"""Black-box test for an installed patched R3, using only the public launcher."""
import argparse, hashlib, json, subprocess, tempfile
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("ctl", type=Path)
p.add_argument("manifest", type=Path, help="original R3 smoke-scene.json")
a = p.parse_args()
ctl = a.ctl.resolve(strict=True)
def call(*args, ok=True):
    proc = subprocess.run([ctl, *args], capture_output=True, text=True)
    assert (proc.returncode == 0) == ok, (args, proc.returncode, proc.stdout, proc.stderr)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

assert "scene.inspect" in call("probe")["data"]["qualified_routes"]
prep = call("scene","prepare","--manifest",a.manifest.resolve())["data"]
src = Path(prep["candidate"])
digest = sha(src)
with tempfile.TemporaryDirectory() as d:
    original = Path(d).resolve()/"fixture.blend"
    original.write_bytes(src.read_bytes())
    original.chmod(0o400)
    signature = lambda: (original.stat().st_size,original.stat().st_mtime_ns,sha(original))
    before = signature()
    def inspect(*args, ok=True):
        return call("scene","inspect","--file",original,*args,ok=ok)
    out = inspect("--expected-sha256",digest)
    data = out["data"]
    assert out["ok"] and out["command"]=="scene.inspect" and data["source_unchanged"]
    assert data["source_file"]==str(original) and data["source_sha256"]==digest
    assert "QABox" in {o["name"] for o in data["objects"]}
    assert any(c["name"]=="QA" and "QABox" in c["objects"] for c in data["collections"])
    assert data["evaluation_context"]["scene"]=="Scene" and "candidate" not in data
    for opts in [(),("--expected-sha256","0"*64),("--expected-sha256",digest,"--manifest",a.manifest)]:
        inspect(*opts,ok=False)
    linked=Path(d).resolve()/"link.blend";linked.symlink_to(original)
    call("scene","inspect","--file",linked,"--expected-sha256",digest,ok=False)
    call("model","inspect","--file",original,"--expected-sha256",digest,ok=False)
    assert signature()==before and {p.name for p in original.parent.iterdir()}=={"link.blend","fixture.blend"}
    worker=ctl.parent.parent/"af-exterior-linux-suite/jobs/common"/out["job_id"]/"worker"
    assert {p.name for p in worker.glob("*.blend")}=={"input.blend"}
print("PASS: probe, scene.prepare, scene.inspect, report content, source hash/mtime, no candidate, five refusals")
