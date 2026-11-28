"""Run inside Blender 5.3: blender -b --factory-startup --disable-autoexec --offline-mode -P camera_motion.py -- request.json."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import bpy


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def texture_state():
    images={}
    for image in bpy.data.images:
        if image.source!="FILE":
            continue
        if image.packed_file:
            images[image.name]={"packed_sha256":hashlib.sha256(image.packed_file.data).hexdigest()}
        else:
            file=Path(bpy.path.abspath(image.filepath,library=image.library)).resolve(strict=True)
            images[image.name]={"file":str(file),"sha256":digest(file)}
    return images


def camera_state(scene, specs):
    old, subframe=scene.frame_current,scene.frame_subframe
    values=[]
    try:
        for spec in specs:
            camera=scene.objects[spec["name"]]
            first,last=spec["frames"]
            for frame in sorted({first,(first+last)//2,last}):
                scene.frame_set(frame)
                values.append([camera.name,frame,list(c for row in camera.matrix_world for c in row),
                               camera.data.lens,camera.data.dof.focus_distance])
    finally:
        scene.frame_set(old,subframe=subframe)
    return values


def execute(req):
    if not bpy.app.background or bpy.data.filepath or not {"--disable-autoexec","--factory-startup","--offline-mode"} <= set(sys.argv):
        raise ValueError("Start an independent offline factory worker")
    package=Path(__file__).resolve().parents[1]/"addons/camera_workflow"
    sys.path.insert(0,str(package))
    from camera_windows_static import register, shot_motion
    register()
    source=Path(req["file"]).resolve(strict=True)
    if source.suffix.lower()!=".blend" or digest(source)!=req["expected_sha256"]:
        raise ValueError("Source identity differs")
    bpy.ops.wm.open_mainfile(filepath=str(source),use_scripts=False,load_ui=False)
    scene=bpy.data.scenes[req["scene"]]
    if scene!=bpy.context.scene:
        bpy.context.window.scene=scene
    report={"source_sha256":req["expected_sha256"],"blender":bpy.app.version_string, "scene":scene.name}
    if req["operation"]=="verify-saved":
        if texture_state()!=req["textures"] or camera_state(scene,req["cameras"])!=req["values"]:
            raise ValueError("Cold-reopened camera samples or texture bindings changed")
        report["verified"]=True
    elif req["operation"]=="diagnose":
        report["frames"]=[]
        for sample in req["samples"]:
            scene.frame_set(sample["frame"])
            camera=scene.objects[sample["camera"]]
            report["frames"].append({**sample,"location":list(camera.matrix_world.translation),
                "lens":camera.data.lens,"focus":camera.data.dof.focus_distance,
                "framing":shot_motion.diagnose(scene,camera,sample["target"],sample.get("bone",""),
                                               sample.get("desired",(0.5,0.5)),sample.get("size_ratio",1))})
    elif req["operation"]=="generate":
        output=Path(req["output"])
        if not output.is_absolute() or output.suffix.lower()!=".blend" or output.exists():
            raise ValueError("Specify a fresh absolute .blend output")
        textures=texture_state()
        declared={str(Path(r["file"]).resolve(strict=True)):r["sha256"] for r in req.get("resources",[])}
        if {r["file"]:r["sha256"] for r in textures.values() if "file" in r}!=declared:
            raise ValueError("External FILE image inventory differs from supplied resources")
        report["cameras"]=[shot_motion.generate(scene,spec).name for spec in req["cameras"]]
        temporary=output.with_name("."+uuid.uuid4().hex+".blend")
        bpy.ops.wm.save_as_mainfile(filepath=str(temporary),copy=True,relative_remap=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as work:
            proof=Path(work)/"cold.json"
            command=Path(work)/"request.json"
            command.write_text(json.dumps({"operation":"verify-saved","file":str(temporary),
                "expected_sha256":digest(temporary),"scene":scene.name,"textures":textures,"cameras":req["cameras"],
                "values":camera_state(scene,req["cameras"]),"report":str(proof)}),encoding="utf-8")
            subprocess.run([bpy.app.binary_path,"-b","--factory-startup","--disable-autoexec","--offline-mode",
                            "-t","2","--python-exit-code","1","-P",str(Path(__file__).resolve()),"--",str(command)],
                           check=True,timeout=300)
            report["independent_reopen"]=json.loads(proof.read_bytes())
        if digest(source)!=req["expected_sha256"] or output.exists() or texture_state()!=textures:
            raise ValueError("Source or destination changed")
        temporary.rename(output)
        report["textures"]=textures
        report["output"]={"file":str(output),"sha256":digest(output)}
    else:
        raise ValueError("Expected generate or diagnose")
    if digest(source)!=req["expected_sha256"]:
        raise ValueError("Source changed during operation")
    return report


if __name__=="__main__":
    req=json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_bytes())
    result=Path(req["report"])
    if result.exists():
        raise FileExistsError(result)
    report=execute(req)
    with result.open("x",encoding="utf-8") as stream:
        json.dump(report,stream,indent=2,allow_nan=False)

