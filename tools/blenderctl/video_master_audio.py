"""Finish a VSE PNG sequence and PCM mix, then verify the delivered AAC stream."""
import argparse
from array import array
from collections import deque
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess


def command(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError(result.stderr[-2000:])
    return result.stderr


def read_ebu(log):
    summary=log.rsplit("Summary:",1)[-1]
    return (float(re.search(r"I:\s+([-\d.]+) LUFS",summary)[1]),
            float(re.search(r"Peak:\s+([-\d.]+) dBFS",summary)[1]), summary)


def audio_transients(ffmpeg, path):
    """Screen decoded PCM for clipping and isolated high-amplitude sample jumps."""
    process=subprocess.Popen([str(ffmpeg),"-v","error","-nostdin","-i",str(path),"-vn","-ac","2",
                              "-ar","48000","-c:a","pcm_f32le","-f","f32le","-"],
                             stdout=subprocess.PIPE)
    previous=[0.0,0.0]
    history=[deque(maxlen=96),deque(maxlen=96)]
    power=[0.0,0.0]
    clips=jumps=index=0
    try:
        while raw:=process.stdout.read(32768):
            samples=array("f")
            samples.frombytes(raw)
            for value in samples:
                channel=index%2
                delta=value-previous[channel]
                window=history[channel]
                if len(window)==window.maxlen:
                    if abs(delta)>max(0.60,12*math.sqrt(max(0,power[channel])/len(window))):
                        jumps+=1
                    power[channel]-=window[0]
                window.append(delta*delta)
                power[channel]+=delta*delta
                previous[channel]=value
                clips+=int(abs(value)>=1)
                index+=1
    finally:
        process.stdout.close()
        code=process.wait()
    if code or not index or clips or jumps:
        raise ValueError(f"PCM verification failed: {code=} {clips=} {jumps=} {index=}")
    return {"sample_rate":48000,"channels":2,"clipped_samples":clips,"isolated_jumps":jumps,
            "jump_threshold":"abs(delta)>max(0.60 FS,12x prior 2 ms delta RMS)"}


def finish(ffmpeg, ffprobe, sequence, audio, output, target, fps, start, count):
    if target not in (-14, -23) or count < 1 or output.exists():
        raise ValueError("Choose a -14/-23 LUFS target, positive frame count and new output")
    # Leave headroom for AAC intersample overshoot. Analyze the actual encoded file below.
    base = f"loudnorm=I={target}:TP=-1.6:LRA=7"
    log = command([ffmpeg, "-hide_banner", "-nostdin", "-i", audio, "-af",
                   base + ":print_format=json", "-f", "null", "-"])
    stats = json.loads(re.findall(r"\{[^{}]*\}", log)[-1])
    fields = (("measured_I","input_i"),("measured_TP","input_tp"),("measured_LRA","input_lra"),
              ("measured_thresh","input_thresh"),("offset","target_offset"))
    for _, key in fields:
        if not math.isfinite(float(stats[key])):
            raise ValueError("Cannot normalize silence or an unmeasurable stream")
    measured = ":".join(name + "=" + stats[key] for name,key in fields)
    samples = round(Fraction(count * 48000, 1) / Fraction(fps))
    # loudnorm/limiter may emit discontinuous timestamps. The VSE mix starts
    # at sample zero, so timestamp the exact trimmed PCM on its sample clock.
    trim = f",aresample=48000,apad,atrim=end_sample={samples},asetpts=N/SR/TB"
    # Image files are display-referred sRGB. Convert transfer and matrix for Rec.709 H.264.
    color = "colorspace=iall=bt709:itrc=srgb:irange=pc:all=bt709:range=tv:fast=0,format=yuv420p"
    import tempfile
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        pending=Path(directory)/"master.mp4"
        attempts=[]
        boost=0.0
        ceiling=-1.8
        for attempt in range(5):
            encoded=Path(directory)/f"audio-{attempt}.m4a"
            limit=10**(ceiling/20)
            filters=(base+":"+measured+":linear=true,aresample=192000,"
                     f"volume={boost:.3f}dB,alimiter=limit={limit:.8f}:level=false:latency=true"+trim)
            command([ffmpeg,"-v","error","-nostdin","-n","-i",audio,"-af",filters,
                     "-ar","48000","-ac","2","-c:a","aac","-b:a","256k",encoded])
            preflight=command([ffmpeg,"-hide_banner","-nostdin","-i",encoded,"-af",
                               "ebur128=peak=true","-f","null","-"])
            loud,top,_=read_ebu(preflight)
            attempts.append({"gain_db":boost,"limiter_ceiling_db":ceiling,"aac_lufs":loud,"aac_dbtp":top})
            if abs(loud-target)<=0.7 and top<=-1:
                break
            boost+=max(-6,min(6,target-loud))
            if top>-1:
                ceiling-=top+1.25
        else:
            raise ValueError(f"Encoded audio cannot meet delivery constraints: {attempts}")
        command([ffmpeg,"-hide_banner","-nostdin","-n","-framerate",fps,"-start_number",start,
                 "-i",sequence,"-i",encoded,"-map","0:v:0","-map","1:a:0","-vf",color,
                 "-c:v","libx264","-crf","17","-preset","veryfast","-colorspace","bt709",
                 "-color_primaries","bt709","-color_trc","bt709","-c:a","copy","-movflags","+faststart",pending])
        audit=command([ffmpeg,"-hide_banner","-nostdin","-i",pending,"-vf","blackdetect=d=0:pix_th=0.02",
                       "-af","ebur128=peak=true","-f","null","-"])
        integrated,peak,summary=read_ebu(audit)
        black=re.findall(r"black_start:([\d.]+) black_end:([\d.]+)",audit)
        details=json.loads(subprocess.check_output([str(ffprobe),"-v","error","-count_frames","-show_streams",
                                                 "-show_format","-of","json",str(pending)]))
        video=next(s for s in details["streams"] if s["codec_type"]=="video")
        sound=next(s for s in details["streams"] if s["codec_type"]=="audio")
        if (abs(integrated-target)>1 or peak>-1 or black or int(video["nb_read_frames"])!=count
                or abs(float(video["duration"])-float(sound["duration"]))>1/48000+0.001):
            raise ValueError(f"Verification failed: {integrated=} {peak=} {black=}, "
                             f"video={video['nb_read_frames']} frames / {video['duration']}s, "
                             f"audio={sound['duration']}s")
        if output.exists():
            raise FileExistsError(output)
        pcm=audio_transients(ffmpeg,pending)
        pending.rename(output)
    return {"target_lufs":target,"integrated_lufs":integrated,"true_peak_dbtp":peak,"black_intervals":black,
            "input_loudnorm":stats,"aac_measurements":attempts,"pcm_screen":pcm,"ffprobe":details,
            "ebur128":summary,"encoded_frames":count}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("ffmpeg","ffprobe","sequence","audio","output","report"):
        p.add_argument("--"+name,required=True,type=Path)
    p.add_argument("--fps",required=True)
    p.add_argument("--target",required=True,choices=(-14,-23),type=int)
    p.add_argument("--start",type=int,default=1)
    p.add_argument("--count",required=True,type=int)
    a=p.parse_args()
    if a.report.exists():
        p.error("Report exists")
    result=finish(a.ffmpeg,a.ffprobe,a.sequence,a.audio,a.output,a.target,a.fps,a.start,a.count)
    with a.report.open("x",encoding="utf-8") as stream:
        json.dump(result,stream,indent=2)

