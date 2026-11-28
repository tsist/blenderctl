"""Native 5.3 shot replacement, transitions and dialogue-driven BGM automation."""
from array import array
import hashlib
import json
import math
from pathlib import Path
import wave

import bpy


def replace_shots(scene, rows):
    """Replace the sequence including any source handles, leaving cuts and modifiers in place."""
    editor=scene.sequence_editor
    lookup={}
    for strip in editor.strips_all:
        key=strip.get("shot_id") or json.loads(strip.get("_vwe_spec","{}")).get("shot_id")
        if key:
            if key in lookup:
                raise ValueError("Duplicate shot in timeline: "+key)
            lookup[key]=strip
    planned=[]
    used=set()
    for row in rows:
        key=row["shot_id"]
        if key in used or key not in lookup:
            raise ValueError("Duplicate or missing shot: "+key)
        used.add(key)
        strip=lookup[key]
        files=row["frames"]
        numbers=list(range(row["frame_start"],row["frame_end"]+1))
        if strip.type!="IMAGE" or strip.lock or len(files)!=len(strip.elements):
            raise ValueError("An unlocked image sequence with all source members is required: "+key)
        cut=list(range(int(strip.frame_final_start),int(strip.frame_final_end)))
        first=int(strip.content_start)
        source_frames=list(range(first,first+len(strip.elements)))
        if cut!=numbers or [x["frame"] for x in files]!=source_frames:
            raise ValueError("Shot cut or frame sequence including source handles differs: "+key)
        paths=[Path(x["path"]).resolve(strict=True) for x in files]
        if len({p.parent for p in paths})!=1:
            raise ValueError("Frame directory differs within shot: "+key)
        for p,item in zip(paths,files):
            with p.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest()!=item["sha256"]:
                    raise ValueError("Media SHA mismatch: "+str(p))
        planned.append((strip,paths,files))
    for strip,paths,files in planned:
        from . import core
        spec = json.loads(strip.get("_vwe_spec","{}"))
        prior = None
        if spec:
            prior = {key: {"baseline": core._read(strip,key), "observed": observed}
                     for key, observed in (("_vwe_baseline",core._actual(scene,strip)),
                                           ("_vwe_native",core.native_strip_record(strip)),
                                           ("_vwe_curves",core._curve_snapshot(scene,strip)))}
        strip.directory=str(paths[0].parent)
        for elem,path in zip(strip.elements,paths):
            elem.filename=path.name
        if spec:
            spec["source"]["files"]=[{"file":str(path),"expected_sha256":member["sha256"]}
                                      for path,member in zip(paths,files)]
            core._store(scene,strip,spec,prior)
    return sorted(used)


def media_snapshot(scene):
    """Hash what the cold VSE scene actually references, including each managed frame and sound."""
    verified={}
    for strip in scene.sequence_editor.strips_all:
        spec=json.loads(strip.get("_vwe_spec","{}"))
        members=spec.get("source",{}).get("files",[])
        if not members:
            continue
        if strip.type=="IMAGE":
            base=Path(bpy.path.abspath(strip.directory))
            paths=[base/elem.filename for elem in strip.elements]
        elif strip.type=="SOUND":
            paths=[Path(bpy.path.abspath(strip.sound.filepath,library=strip.sound.library))]
        elif strip.type=="MOVIE":
            paths=[Path(bpy.path.abspath(strip.filepath))]
        else:
            raise ValueError("Unsupported source-bearing VSE strip: "+strip.name)
        if len(paths)!=len(members):
            raise ValueError("Saved media membership differs for "+strip.name)
        for path,member in zip(paths,members):
            actual=path.resolve(strict=True)
            if str(actual) not in verified:
                with actual.open("rb") as file:
                    verified[str(actual)]=hashlib.file_digest(file,"sha256").hexdigest()
            if verified[str(actual)]!=member["expected_sha256"]:
                raise ValueError("Saved media SHA mismatch: "+str(actual))
    return verified


def compositor_modifier(strip, group=None, exposure=0):
    if not group:
        group=bpy.data.node_groups.new("VSE exposure","CompositorNodeTree")
        for direction in ("INPUT","OUTPUT"):
            group.interface.new_socket(name="Image",in_out=direction,socket_type="NodeSocketColor")
        source=group.nodes.new("NodeGroupInput")
        sink=group.nodes.new("NodeGroupOutput")
        grade=group.nodes.new("CompositorNodeExposure")
        grade.inputs["Exposure"].default_value=exposure
        group.links.new(source.outputs["Image"],grade.inputs["Image"])
        group.links.new(grade.outputs["Image"],sink.inputs["Image"])
    if group.bl_idname!="CompositorNodeTree":
        raise ValueError("Expected compositor node tree")
    modifier=strip.modifiers.new("Shot finish", "COMPOSITOR")
    modifier.node_group=group
    return modifier


def cross(scene, left, right, kind, channel):
    if kind not in {"CROSS","GAMMA_CROSS"} or min(left.frame_final_end,right.frame_final_end)<=max(left.frame_final_start,right.frame_final_start):
        raise ValueError("A cross requires overlapping inputs")
    if channel<=max(left.channel,right.channel):
        raise ValueError("Place transitions on a channel above their inputs")
    return scene.sequence_editor.strips.new_effect(name=f"{left.name} to {right.name}",type=kind,
        frame_start=int(max(left.frame_final_start,right.frame_final_start)),channel=channel,input1=left,input2=right)


def duck(scene, music, dialogue, floor_db=-13, threshold_db=-35, attack_ms=45, release_ms=180):
    """Derive editable native BGM volume keys from frame-window PCM dialogue RMS."""
    if music.type!="SOUND" or dialogue.type!="SOUND" or music.lock or music==dialogue:
        raise ValueError("Two independent sound strips and an unlocked music track required")
    if floor_db>0 or attack_ms<=0 or release_ms<=0:
        raise ValueError("Invalid ducking range or timing")
    anim=scene.animation_data
    music_path=music.path_from_id("volume")
    if anim:
        if any(d.data_path==music_path for d in anim.drivers) or len(anim.nla_tracks):
            raise ValueError("BGM automation requires a track without existing drivers or NLA")
        from .core import _curves
        if any(curve.data_path==music_path for curve in _curves(scene)):
            raise ValueError("BGM already has volume automation")
    fps=scene.render.fps/scene.render.fps_base
    base=music.volume
    path=Path(bpy.path.abspath(dialogue.sound.filepath))
    result=[]
    gain=1.0
    start_frame=scene.frame_current
    start_subframe=scene.frame_subframe
    frames=range(int(music.frame_final_start),int(music.frame_final_end))
    dialogue_levels={}
    if anim:
        try:
            for frame in frames:
                scene.frame_set(frame)
                dialogue_levels[frame]=0 if dialogue.mute else dialogue.volume
        finally:
            scene.frame_set(start_frame,subframe=start_subframe)
    with wave.open(str(path),"rb") as stream:
        if stream.getsampwidth()!=2 or stream.getcomptype()!="NONE":
            raise ValueError("Decode the dialogue stem to 16-bit PCM WAV")
        rate=stream.getframerate()
        for frame in frames:
            rms=0
            if dialogue.frame_final_start<=frame<dialogue.frame_final_end:
                index=round((frame-dialogue.content_start)*rate/fps)
                end=round((frame+1-dialogue.content_start)*rate/fps)
                if 0<=index<stream.getnframes():
                    stream.setpos(index)
                    data=array("h",stream.readframes(end-index))
                    if data:
                        level=dialogue_levels.get(frame,0 if dialogue.mute else dialogue.volume)
                        rms=level*math.sqrt(sum(x*x for x in data)/len(data))/32768
            level=20*math.log10(max(1e-8,rms))
            amount=max(0,min(1,(level-threshold_db)/12))
            target=10**(floor_db*amount/20)
            time_ms=attack_ms if target<gain else release_ms
            gain=target+(gain-target)*math.exp(-1000/(fps*time_ms))
            music.volume=base*gain
            music.keyframe_insert(data_path="volume",frame=frame)
            result.append(round(gain,6))
    return result

