"""Native Linux static candidate smoke. Use a checkout on qualified local storage."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(blender):
    env = {k:v for k,v in os.environ.items() if not k.startswith("BLENDER_")}
    with tempfile.TemporaryDirectory(dir=os.environ.get("GUARD_TEST_ROOT", "/tmp")) as name:
        work = Path(name)
        source = work / "fixture.blend"
        env["BLENDER_USER_RESOURCES"] = str(work / "user")
        subprocess.run([blender, "-b", "--factory-startup", "--disable-autoexec", "--offline-mode",
                        "--python-exit-code", "23", "--python-expr",
                        "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); "
                        "bpy.ops.mesh.primitive_cube_add(); bpy.context.object.name='FixtureCube'; "
                        f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})"], env=env, check=True)
        before = digest(source)
        context = dict(scene="Scene", view_layer="ViewLayer", frame=1)
        geometry = dict(schema_version="1.0", context=context, targets=[
            dict(id="fence", object="FixtureCube", modifier="Fence", action="build", recipe="curve_fence")
        ], preview={"enabled": False})
        lighting = dict(version="1.0", scene="Scene", view_layer="ViewLayer", rig_id="Rig",
                        action="create", targets=["FixtureCube"], mode="adapt", anchor="world",
                        lights=[dict(id="Key", role="key", type="AREA", location=[2,-3,5],
                                     energy=300, color=[1,.8,.6], size=2)])
        def run(unit, manifest, expected=before, **extra):
            req = work / "request.json"
            req.write_text(json.dumps(dict(schema_version="1.0", command=unit+".run",
                          params=dict(file=str(source), expected_sha256=expected, resources=[],
                                      manifest=manifest, **extra))), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(ROOT / "tools/blenderctl/cli.py"),
                                   "--blender", blender, "--jobs-dir", str(work / "jobs"),
                                   "--timeout", "90", "request", str(req)],
                                  env=env, capture_output=True, text=True, timeout=110)
            assert digest(source) == before
            return proc.returncode, json.loads(proc.stdout)

        for unit, manifest in (("geometry", geometry), ("lighting", lighting)):
            rc, result = run(unit, manifest)
            assert rc == 0 and result["ok"], result
            data = result["data"]
            assert data["status"] == "passed" and data["source_unchanged"] and not data["previews"]
            assert len(data["input_hashes"]) > 128
            if unit == "geometry":
                assert data["targets"][0]["status"] == "passed"
                assert data["verification"]["independent_reopen"]["status"] == "passed"
                assert digest(Path(data["candidate"])) == data["candidate_sha256"]
            else:
                check = data["verification"]
                assert check["status"] == check["non_target_protection"] == "passed"
                assert check["inspection"]["rigs"][0]["lights"][0]["energy"] == 300
                assert digest(Path(data["candidate"]["file"])) == data["candidate"]["sha256"]
            rc, bad = run(unit, manifest, expected="0"*64)
            assert rc != 0 and bad["error"]["code"] == "CONFLICT", bad
            print("PASS:", unit, "candidate, independent reopen, no previews, wrong hash rejected")

        # Asking for a preview must fail without a Blender worker or accepted data.
        rc, bad = run("geometry", {**geometry, "preview":{"enabled":True}})
        assert rc != 0 and bad["error"]["code"] == "UNSUPPORTED" and bad["data"] is None, bad
        rc, bad = run("lighting", lighting, preview=dict(engines=["CYCLES"], device={"backend":"CPU"},
                                                         width=64, height=64, samples=1))
        assert rc != 0 and bad["error"]["code"] == "UNSUPPORTED" and bad["data"] is None, bad
        print("PASS: preview requests refused before worker launch")


if __name__ == "__main__":
    main(str(Path(sys.argv[1]).resolve(strict=True)))
