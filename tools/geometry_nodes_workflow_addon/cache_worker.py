"""Bake or package native Geometry Nodes caches in a separate Blender process."""

import json
from pathlib import Path
import re
import sys
from zipfile import ZIP_DEFLATED, ZipFile


def write_json(path, payload):
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.replace(path)


def bind_cache(modifier, root, rows):
    items = {item.bake_id: item for item in modifier.bakes}
    if set(items) != {row["id"] for row in rows}:
        raise ValueError("节点缓存标识已变更")
    modifier.bake_target = "DISK"
    modifier.bake_directory = str(root)
    for row in rows:
        item = items[row["id"]]
        item.bake_target = "DISK"
        item.bake_mode = "ANIMATION"
        item.use_custom_path = True
        item.directory = str(root / row["folder"])
        item.use_custom_simulation_frame_range = True
        item.frame_start, item.frame_end = row["start"], row["end"]


def verify_frames(directory, start, end):
    frames = set()
    for path in (directory / "meta").glob("*.json"):
        match = re.fullmatch(r"(-?\d+)_0+\.json", path.name)
        if not match:
            raise ValueError(f"无法识别缓存帧：{path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 3 or not isinstance(data.get("items"), dict):
            raise ValueError(f"无法识别缓存记录：{path.name}")
        frames.add(int(match[1]))
    if frames != set(range(start, end + 1)):
        raise ValueError(f"缓存只包含 {len(frames)} 帧，预期 {end - start + 1} 帧")


def bake(request, update):
    import bpy

    scene = bpy.data.scenes[request["scene"]]
    view_layer = scene.view_layers[request["view_layer"]]
    obj = view_layer.objects.get(request["object"])
    if obj is None:
        raise ValueError("快照中未找到烘焙目标")
    modifier = obj.modifiers[request["modifier"]]
    root = Path(request["root"])
    rows = request["bakes"]
    bind_cache(modifier, root, rows)
    update(0.15, "正在建立烘焙快照")
    with bpy.context.temp_override(scene=scene, view_layer=view_layer, object=obj, active_object=obj,
                                   selected_objects=[obj], selected_editable_objects=[obj]):
        for index, row in enumerate(rows):
            update(0.2 + 0.7 * index / len(rows), f"正在烘焙节点 {index + 1}/{len(rows)}")
            scene.frame_set(row["start"])
            result = bpy.ops.object.geometry_node_bake_single(
                session_uid=obj.session_uid, modifier_name=modifier.name, bake_id=row["id"])
            if result != {"FINISHED"}:
                raise RuntimeError(f"节点 {index + 1} 烘焙未完成")
            verify_frames(root / row["folder"], row["start"], row["end"])
    write_json(root / "cache-receipt.json", request)
    return {"root": str(root), "bakes": rows}


def package(request, update):
    root = Path(request["root"])
    output = root / "几何节点缓存.zip"
    partial = root / "cache.zip.partial"
    rows = request["bakes"]
    with ZipFile(partial, "x", compression=ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("cache-receipt.json", json.dumps(
            {**request, "bakes": [{key: value for key, value in row.items() if key != "source"} for row in rows]},
            ensure_ascii=False, indent=2))
        for index, row in enumerate(rows):
            source = Path(row["source"])
            verify_frames(source, row["start"], row["end"])
            update(0.15 + 0.75 * index / len(rows), f"正在封装节点 {index + 1}/{len(rows)} 的缓存")
            for subfolder in ("meta", "blobs"):
                for member in sorted((source / subfolder).glob("*")):
                    if member.is_file():
                        archive.write(member, f'{row["folder"]}/{subfolder}/{member.name}')
    partial.rename(output)
    return {"archive": str(output)}


def main():
    request_file, status_file, result_file = map(Path, sys.argv[sys.argv.index("--") + 1:])
    request = json.loads(request_file.read_text(encoding="utf-8"))

    def update(progress, detail):
        write_json(status_file, {"progress": progress, "detail": detail})

    try:
        action = {"bake": bake, "export": package}[request["action"]]
        result = action(request, update)
        write_json(result_file, {"ok": True, **result})
    except Exception as exc:
        write_json(result_file, {"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()
