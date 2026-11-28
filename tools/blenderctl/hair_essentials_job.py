"""Generic blenderctl extension.run entry. All outputs go into this job's fresh directory."""
import hashlib
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hair_essentials


def execute(request, folder):
    source = Path(request["file"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != request["expected_sha256"]:
        raise ValueError("Source blend fingerprint changed")
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    action = request["operation"]
    if action == "assemble":
        guide = scene.objects[request["guide"]]
        material = bpy.data.materials[request["material"]] if request.get("material") else None
        groom = hair_essentials.assemble(guide, request["name"],request.get("settings"),material)
        graph = bpy.context.evaluated_depsgraph_get()
        geo = groom.evaluated_get(graph).evaluated_geometry()
        if not geo.curves or not len(geo.curves.curves):
            raise ValueError("Hair Essentials produced no evaluated strands")
        result = {"object":groom.name,"strands":len(geo.curves.curves),
                  "surface":guide.data.surface.name,"stages":[m.name for m in groom.modifiers]}
        bpy.ops.wm.save_as_mainfile(filepath=str(folder/"output-groom.blend"),compress=True)
    elif action == "export_cards":
        scene.frame_set(request.get("frame",scene.frame_current))
        groom = scene.objects[request["groom"]]
        cards = hair_essentials.cards_from_curves(groom,request["name"],
            limit=request.get("card_count",160),width=request.get("card_width",.002))
        for selected in list(bpy.context.selected_objects):
            selected.select_set(False)
        cards.select_set(True)
        bpy.context.view_layer.objects.active = cards
        bpy.ops.export_scene.gltf(filepath=str(folder/"output-cards.glb"),export_format="GLB",
                                  use_selection=True,export_animations=False,export_materials="EXPORT")
        result = {"object":cards.name,"frame":scene.frame_current,"quads":len(cards.data.polygons),
                  "deformation": "static evaluated cards; no skin weights or animation"}
    else:
        raise ValueError("Expected assemble or export_cards")
    result["source_sha256"] = request["expected_sha256"]
    output = folder / ("output-groom.blend" if action == "assemble" else "output-cards.glb")
    with output.open("rb") as handle:
        result["output"] = {"file": str(output), "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
                            "bytes": output.stat().st_size}
    (folder/"output-hair.json").write_text(json.dumps(result,indent=2),encoding="utf8")
    return result


if __name__ == "__main__":
    result = execute(parameters, Path(job_directory))
