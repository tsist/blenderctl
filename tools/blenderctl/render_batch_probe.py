"""Read the frozen Blender snapshot and optionally recapture a known cache."""
from pathlib import Path
import sys
import math

import bpy

cli_directory = Path(__file__).resolve().parent
sys.path[:0] = [str(cli_directory), str(cli_directory.parent)]

from protocol import atomic_json, digest, read_json
from render_batch_delivery import frame_rate
from source_render_settings import assert_source_preservable, capture_source

request_path = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
request = read_json(request_path)
source = Path(request["file"])
if Path(bpy.data.filepath).resolve() != source or digest(source) != request["expected_sha256"]:
    raise RuntimeError("Source snapshot does not match the preflight request")
shot = request["shot"]
scene = bpy.data.scenes.get(shot["scene"])
layer = scene.view_layers.get(shot["view_layer"]) if scene else None
if layer is None:
    raise RuntimeError("The requested scene/view layer does not exist")
camera = layer.objects.get(shot["camera"])
if camera is None or camera.type != "CAMERA":
    raise RuntimeError("The requested camera is outside the view layer")
start, end = shot["frame_start"], shot["frame_end"]
if start < scene.frame_start or end > scene.frame_end:
    raise RuntimeError("Shot frames must be within the saved scene frame range")
if not math.isclose(scene.render.fps / scene.render.fps_base, float(frame_rate(request['fps'])), rel_tol=1e-7):
    raise RuntimeError("Saved frame rate differs from requested review frame rate")
assert_source_preservable(scene, layer, camera, (start, end), allow_compositor=True)
source_settings = capture_source(scene, layer)
if source_settings["effective"]["display_device"] != "sRGB":
    raise RuntimeError("The SDR review encoder requires an sRGB display rendering")
if request["delivery_aspect"][0] * scene.render.resolution_y != request["delivery_aspect"][1] * scene.render.resolution_x:
    raise RuntimeError("Requested dimensions change the saved camera's image aspect")
layer.update_render_passes()
nodes = bpy.data.node_groups.new('Batch pass preflight', 'CompositorNodeTree')
node = nodes.nodes.new('CompositorNodeRLayers')
node.scene = scene
node.layer = layer.name
native_passes = sorted({socket.name for socket in node.outputs if socket.enabled})
bpy.data.node_groups.remove(nodes)
effects = [{'name': effect.name, 'enabled_render': bool(effect.enable_for_render)} for effect in scene.compositor_effects]
output = {"source_settings": source_settings, "build": bpy.app.build_hash.decode(), "cache_manifest": None,
          "native_passes": native_passes, "scene_effects": effects}
cache = request.get("cache")
if cache:
    from geometry_identity import implementation_sha256
    from geometry_temporal import capture_manifest

    if (bpy.context.scene != scene or bpy.context.view_layer != layer):
        raise RuntimeError("A managed cache requires the saved active scene and view layer")
    cache_dir = request_path.parent / "cache-capture"
    cache_dir.mkdir()
    targets = [{key: row[key] for key in ("id", "object", "modifier")} for row in cache["targets"]]
    temporal = {
        "frame_start": cache["frame_start"], "frame_end": cache["frame_end"], "fps": cache["fps"],
        "observe_frames": sorted({start, (start + end) // 2, end}),
        "storage": "DISK",
        "bakes": [{"target": row["target"], "node": row["node_id"], "kind": row["kind"],
                   "group_path": row["group_path"]} for row in cache["bakes"]],
    }
    receipt = capture_manifest(temporal, targets, cache["observations"], cache_dir,
                               source, implementation_sha256())
    captured = read_json(receipt["file"])
    if captured["files"] != cache["files"] or not Path(captured["root"]).is_relative_to(source.parent):
        raise RuntimeError("Cache capture resolved different members or a location outside the snapshot")
    output["cache_manifest"] = receipt

atomic_json(request_path.with_name("source-settings.json"), output)

