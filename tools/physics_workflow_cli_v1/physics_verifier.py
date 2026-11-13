"""Independent cold-reopen verifier executed in a fresh Blender process with use_scripts=False."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy


def _read_args() -> dict[str, str]:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Verifier requires '-- <verify_json> <output_json>'")
    rest = argv[argv.index("--") + 1 :]
    return {"spec_path": rest[0], "result_path": rest[1]}


def _eval_summary(obj: bpy.types.Object, frame: int) -> dict:
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(dg)

    inst_pts = []
    for inst in dg.object_instances:
        if not inst.is_instance:
            continue
        p = inst.parent
        if p == eval_obj or (p is not None and p.original == obj):
            loc = inst.matrix_world.translation
            inst_pts.append((float(loc.x), float(loc.y), float(loc.z)))

    mesh = eval_obj.to_mesh()
    vert_pts = []
    face_count = 0
    if mesh is not None:
        face_count = len(mesh.polygons)
        mat = eval_obj.matrix_world
        for v in mesh.vertices:
            w = mat @ v.co
            vert_pts.append((float(w.x), float(w.y), float(w.z)))
        eval_obj.to_mesh_clear()

    pts = inst_pts if (len(inst_pts) > 1 or (inst_pts and not vert_pts)) else vert_pts
    if pts:
        centroid = [
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
            sum(p[2] for p in pts) / len(pts),
        ]
    else:
        centroid = [0.0, 0.0, 0.0]

    return {
        "frame": frame,
        "instance_count": len(inst_pts),
        "vertex_count": len(vert_pts),
        "face_count": face_count,
        "centroid": centroid,
    }


def main() -> None:
    args = _read_args()
    spec = json.loads(Path(args["spec_path"]).read_text(encoding="utf-8"))
    res_path = Path(args["result_path"]).resolve()

    blend_path = Path(spec["output_blend"]).resolve()
    bpy.ops.wm.open_mainfile(filepath=str(blend_path), use_scripts=False)

    obj = bpy.data.objects.get(spec["target_object"])
    if obj is None:
        out = {"passed": False, "error": f"Target object missing after cold reopen: {spec['target_object']}"}
        res_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        sys.exit(1)

    f_start = int(spec["frame_start"])
    f_end = int(spec["frame_end"])
    f_mid = (f_start + f_end) // 2
    scrambled = [f_end, f_start, f_mid, f_end]

    samples = []
    for f in scrambled:
        samples.append(_eval_summary(obj, f))

    # Assert repeated read of f_end yields exact same centroid and counts
    first_end = samples[0]
    repeat_end = samples[-1]
    max_centroid_diff = max(
        abs(first_end["centroid"][i] - repeat_end["centroid"][i])
        for i in range(3)
    )
    expected = spec.get("expected_end_summary")
    expected_diff = 0.0
    if expected:
        expected_diff = max(
            abs(first_end["centroid"][i] - expected["centroid"][i])
            for i in range(3)
        )

    passed = (
        max_centroid_diff < 1e-4
        and expected_diff < 1e-3
        and first_end["instance_count"] == repeat_end["instance_count"]
        and first_end["vertex_count"] == repeat_end["vertex_count"]
    )

    out = {
        "passed": bool(passed),
        "scrambled_frames": scrambled,
        "max_repeat_centroid_diff": max_centroid_diff,
        "max_expected_centroid_diff": expected_diff,
        "samples": samples,
    }
    res_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
