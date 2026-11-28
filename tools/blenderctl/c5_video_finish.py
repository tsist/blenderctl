"""Isolated Blender 5.3 worker for preparing, conforming and rendering a native VSE timeline."""
import hashlib
import json
from pathlib import Path
import sys
import uuid

import bpy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"addons/video_editing_workflow"))
from video_editing_workflow_addon import core, finishing


def sha(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file,"sha256").hexdigest()


def save(path):
    if path.exists() or not path.is_absolute() or path.suffix.lower()!=".blend":
        raise ValueError("Supply a new absolute .blend")
    temp=path.with_name("."+uuid.uuid4().hex+".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(temp),copy=True,relative_remap=True)
    temp.rename(path)
    return {"file":str(path),"sha256":sha(path)}


def run(req):
    if not bpy.app.background or bpy.data.filepath or not {"--disable-autoexec","--factory-startup","--offline-mode"}<=set(sys.argv):
        raise ValueError("Start a fresh offline worker")
    op=req["operation"]
    if op=="rough":
        for member in req["resources"]:
            if sha(member["file"])!=member["expected_sha256"]:
                raise ValueError("Media changed: "+member["file"])
        created=core.apply(req["manifest"])
        scene=bpy.data.scenes[created["scene"]]
        bpy.context.window.scene=scene
        scene.view_settings.view_transform="Standard"
        strips=scene.sequence_editor.strips
        for t in req.get("transitions",[]):
            finishing.cross(scene,strips[t["left"]],strips[t["right"]],t["kind"],t["channel"])
        for name in req["picture_strips"]:
            finishing.compositor_modifier(strips[name],exposure=req.get("exposure",0))
            balance=strips[name].modifiers.new("Color balance","COLOR_BALANCE")
            balance.color_balance.gain=req.get("gain",(1,1,1))
        for name,xy in req.get("subtitle_positions",{}).items():
            strips[name].location=xy
        duck=finishing.duck(scene,strips[req["music"]],strips[req["dialogue"]])
        scene.frame_set(scene.frame_start)
        return {"duck_gain":duck,"candidate":save(Path(req["output"]))}
    ref=req["source"]
    if sha(ref["file"])!=ref["sha256"]:
        raise ValueError("VSE source changed")
    bpy.ops.wm.open_mainfile(filepath=ref["file"],use_scripts=False,load_ui=False)
    scene=bpy.data.scenes[req["scene"]]
    bpy.context.window.scene=scene
    if op=="conform":
        finishing.media_snapshot(scene)
        replaced=finishing.replace_shots(scene,req["shots"])
        for row in req["shots"]:
            if "proxy_scale" in row:
                strip=next(s for s in scene.sequence_editor.strips_all if s.name==row["shot_id"])
                for axis in ("scale_x","scale_y"):
                    setattr(strip.transform,axis,getattr(strip.transform,axis)*row["proxy_scale"])
        if "review_percentage" in req:
            scene.render.resolution_percentage=req["review_percentage"]
        return {"replaced":replaced,"candidate":save(Path(req["output"]))}
    if op=="export":
        media=finishing.media_snapshot(scene)
        out=Path(req["directory"])
        out.mkdir(exist_ok=False)
        scene.render.image_settings.file_format="PNG"
        scene.render.image_settings.color_mode="RGB"
        scene.render.image_settings.color_depth="8"
        # Turn off stamp in user files only on this render worker.
        scene.render.use_stamp=False
        scene.render.filepath=str(out/"frame-")
        bpy.ops.render.render(animation=True,scene=scene.name)
        mix=out/"mix.wav"
        if bpy.ops.sound.mixdown(filepath=str(mix),container="WAV",codec="PCM",format="S16",
                                 accuracy=256)!= {"FINISHED"}:
            raise ValueError("Native VSE mixdown failed")
        if finishing.media_snapshot(scene)!=media:
            raise ValueError("Media changed during the export")
        return {"frame_count":len(list(out.glob("frame-*.png"))),"audio":str(mix),"media":media,
                "strips":[{"name":s.name,"type":s.type,"range":[s.frame_final_start,s.frame_final_end],
                           "modifiers":[m.type for m in s.modifiers]} for s in scene.sequence_editor.strips]}
    raise ValueError("Expected rough, conform or export")


if __name__=="__main__":
    request=json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_bytes())
    receipt=Path(request["report"])
    if receipt.exists():
        raise FileExistsError(receipt)
    result=run(request)
    with receipt.open("x",encoding="utf-8") as file:
        json.dump(result,file,indent=2,allow_nan=False)

