"""Native CLI smoke; pass Blender 5.3.0 executable and a new private tmpfs directory."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

blender = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).absolute()
root.mkdir(mode=0o700)
cli = Path(__file__).resolve().parents[2] / "tools/blenderctl/cli.py"
subprocess.run([str(blender), "-b", "-t", "1", "--factory-startup", "--disable-autoexec",
                "--python-exit-code", "2", "-P", str(Path(__file__).with_name("fixture.py")),
                "--", str(root)], check=True)
clean = root / "clean.blend"
linked = root / "linked.blend"
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
expected = sha(clean)
spec = root / "spec.json"
spec.write_text(json.dumps({"scene":"Scene", "view_layer":"ViewLayer", "frames":[1,11],
                            "objects":[{"name":"TestRig","bones":["Bone"]}]}))


def run(name, args, failure=None):
    result = subprocess.run([sys.executable, str(cli), "--blender", str(blender), "--jobs-dir",
                             str(root / "jobs"), *map(str,args)], capture_output=True, text=True)
    data = json.loads(result.stdout)
    (root / (name + ".json")).write_text(result.stdout)
    assert (not result.returncode and data["ok"]) if not failure else (
        result.returncode and not data["ok"] and data["error"]["code"] == failure), data
    return data["data"]


result = run("inspect", ["rig","inspect",clean,"--expected-sha256",expected])
assert [(r["object"], [b["name"] for b in r["bones"]]) for r in result["rigs"]] == [("TestRig", ["Bone"])]
result = run("sample", ["animation","sample",clean,"--manifest",spec,"--expected-sha256",expected])
assert [(r["frame"], r["objects"][0]["bones"]["Bone"]["head_world"]) for r in result["frames"]] == [
    (1, [0.,0.,0.]), (11, [3.,0.,0.])]
assert result["source_unchanged"] and sha(clean) == expected == result["source_sha256"]
run("wrong-sha", ["rig","inspect",clean,"--expected-sha256","0"*64], "CONFLICT")
run("no-sha", ["rig","inspect",clean], "UNSUPPORTED")
run("linked", ["animation","sample",linked,"--expected-sha256",sha(linked),"--manifest",spec], "UNSUPPORTED")
alias = root / "alias.blend"
alias.symlink_to(clean)
run("symlink", ["rig","inspect",alias,"--expected-sha256",expected], "CONFLICT")
with clean.open("r+b"):
    run("open-writer", ["rig","inspect",clean,"--expected-sha256",expected], "CONFLICT")
print("Native rig/animation reads passed; 2 positive, 5 negative checks.")
