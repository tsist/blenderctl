"""Encode ordered, verified sRGB frames and certify the review movie."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import zlib
import tempfile

COLOR_FILTER = (
    "format=rgb24,zscale=matrixin=gbr:primariesin=bt709:"
    "transferin=iec61966-2-1:rangein=full:matrix=bt709:"
    "primaries=bt709:transfer=bt709:range=limited:dither=error_diffusion,format=yuv420p"
)


def run_tool(executable, options, directory, seconds):
    done = subprocess.run([str(executable), *options], cwd=directory, check=False,
                          capture_output=True, timeout=seconds,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        tail = done.stderr.decode("utf-8", "replace")[-1400:].strip()
        raise RuntimeError(f"{Path(executable).name} failed ({done.returncode}): {tail}")
    return done.stdout


def preflight_tools(ffmpeg, ffprobe, directory):
    filters = run_tool(ffmpeg, ["-hide_banner", "-filters"], directory, 20).decode("utf-8", "replace")
    encoders = run_tool(ffmpeg, ["-hide_banner", "-encoders"], directory, 20).decode("utf-8", "replace")
    run_tool(ffprobe, ["-version"], directory, 20)
    if "zscale" not in filters.split() or "libx264" not in encoders.split():
        raise RuntimeError("FFmpeg requires zscale and libx264 for BT.709 review output")


def verify_video(ffmpeg, ffprobe, name, directory, count, width, height, fps):
    raw = run_tool(ffprobe, [
        "-v", "error", "-show_streams", "-show_frames", "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,color_range,color_transfer,color_space,color_primaries,avg_frame_rate,time_base:frame=best_effort_timestamp",
        "-of", "json", name,
    ], directory, max(60, count))
    info = json.loads(raw)
    streams = info.get("streams", [])
    expected = {
        "codec_type": "video", "codec_name": "h264", "width": width, "height": height,
        "pix_fmt": "yuv420p", "avg_frame_rate": f"{fps.numerator}/{fps.denominator}",
        "color_range": "tv", "color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709",
    }
    if len(streams) != 1 or any(streams[0].get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Review stream differs from the delivery profile: {streams}")
    time_base = Fraction(streams[0]["time_base"])
    stamps = [int(frame["best_effort_timestamp"]) * time_base for frame in info.get("frames", [])]
    if stamps != [Fraction(index, fps) for index in range(count)]:
        raise RuntimeError("Review frame count or playback timestamps differ from the shot")
    run_tool(ffmpeg, ["-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                      "-i", name, "-map", "0:v:0", "-f", "null", "-"], directory, max(60, count))
    return {"primaries": "bt709", "transfer": "bt709", "matrix": "bt709", "range": "tv",
            "conversion": "full sRGB to limited BT.709 using zscale"}


def encode_sequence(frames, video, *, ffmpeg, ffprobe, width, height, fps=24):
    """Publish one reviewed movie after checking every decoded frame and timestamp."""
    output = Path(video).resolve()
    paths = [Path(frame).resolve() for frame in frames]
    fps = Fraction(fps)
    if not paths or not 1 <= fps <= 120 or any(value <= 0 or value % 2 for value in (width, height)):
        raise ValueError("Review requires even dimensions and a frame rate within 1..120")
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Review destination must be a new MP4 path")
    if any(path.suffix.lower() != ".png" or not path.is_file() for path in paths):
        raise ValueError("Review frames must be existing verified PNGs")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="review-") as workspace:
        stage = Path(workspace)
        for index, path in enumerate(paths, start=1):
            frame = stage / f"frame-{index:06d}.png"
            try:
                os.link(path, frame)
            except OSError:
                shutil.copyfile(path, frame)
        name = "candidate.mp4"
        run_tool(ffmpeg, [
            "-nostdin", "-v", "error", "-xerror", "-n", "-framerate", str(fps), "-start_number", "1",
            "-i", "frame-%06d.png", "-map", "0:v:0", "-frames:v", str(len(paths)),
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1," + COLOR_FILTER,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-movflags", "+faststart+write_colr", "-an", name,
        ], stage, max(120, len(paths)))
        color = verify_video(ffmpeg, ffprobe, name, stage, len(paths), width, height, fps)
        candidate = stage / name
        with candidate.open("rb+") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            os.fsync(handle.fileno())
        # A hard link publishes the already verified bytes atomically and cannot replace an existing file.
        os.link(candidate, output)
    rate = fps.numerator if fps.denominator == 1 else [fps.numerator, fps.denominator]
    return {"path": str(output), "width": width, "height": height, "fps": rate, "frames": len(paths),
            "sha256": digest, "color": color, "codec": "h264"}


# Five-by-seven glyphs for diagnostic cards; no host font or image library needed.
_GLYPHS = {
    "A": "0E111F11111111", "B": "1E111E1111111E", "C": "0F10101010100F",
    "D": "1E11111111111E", "E": "1F101E1010101F", "F": "1F101E10101010",
    "G": "0F10101711110F", "H": "1111111F111111", "I": "0E04040404040E",
    "J": "0702020202120C", "K": "11121418141211", "L": "1010101010101F",
    "M": "111B1515111111", "N": "11191513111111", "O": "0E11111111110E",
    "P": "1E11111E101010", "Q": "0E11111115120D", "R": "1E11111E141211",
    "S": "0F10100E01011E", "T": "1F040404040404", "U": "1111111111110E",
    "V": "11111111110A04", "W": "1111111515150A", "X": "11110A040A1111",
    "Y": "11110A04040404", "Z": "1F01020408101F", "0": "0E11131519110E",
    "1": "040C040404040E", "2": "0E11010204081F", "3": "1E01010601011E",
    "4": "02060A121F0202", "5": "1F101E0101011E", "6": "0E10101E11110E",
    "7": "1F010204080808", "8": "0E11110E11110E", "9": "0E11110F01010E",
    "-": "0000001F000000", "_": "0000000000001F", " ": "00000000000000",
}


def placeholder(card, shot_id, *, width, height):
    """Write one RGB failure card for every frame of an unfinished timeline slot."""
    if width < 64 or height < 48:
        raise ValueError("Review dimensions are too small for a diagnostic card")
    identifier = shot_id.upper()
    if not identifier or any(char not in _GLYPHS or char == " " for char in identifier):
        raise ValueError("Invalid shot id for a diagnostic card")
    margin = max(4, width // 24)
    background = bytes((32, 33, 40))
    orange = bytes((249, 173, 108))
    white = bytes((250, 243, 232))
    row_bytes = width * 3
    canvas = bytearray(background * width * height)
    canvas[:row_bytes * 4] = orange * width * 4
    canvas[-row_bytes * 4:] = orange * width * 4

    def label(text, y, scale, colour):
        first = (width - ((6 * len(text) - 1) * scale)) // 2
        for index, char in enumerate(text):
            bits = bytes.fromhex(_GLYPHS[char])
            for line, mask in enumerate(bits):
                for column in range(5):
                    if mask & (16 >> column):
                        x = first + (index * 6 + column) * scale
                        for offset in range(scale):
                            start = ((y + line * scale + offset) * width + x) * 3
                            canvas[start:start + scale * 3] = colour * scale

    headline = "INCOMPLETE"
    headline_scale = max(1, min(height // 22, (width - 2 * margin) // (len(headline) * 6)))
    id_scale = max(1, min(height // 24, (width - 2 * margin) // (len(identifier) * 6)))
    if (len(identifier) * 6 - 1) * id_scale > width:
        raise ValueError("Shot identity does not fit the review dimensions")
    label(headline, height // 2 - 7 * headline_scale - max(3, height // 19), headline_scale, orange)
    label(identifier, height // 2 + max(3, height // 19), id_scale, white)

    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value +
                struct.pack(">I", zlib.crc32(kind + value)))

    scanlines = b"".join(b"\0" + canvas[start:start + row_bytes]
                         for start in range(0, len(canvas), row_bytes))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with Path(card).open("xb") as file:
        file.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
                   chunk(b"IDAT", zlib.compress(scanlines, 4)) + chunk(b"IEND", b""))
