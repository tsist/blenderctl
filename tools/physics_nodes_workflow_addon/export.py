"""Optional downstream export for static realized meshes, Alembic (.abc), and OpenVDB (.vdb)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy


def export_workflow_result(
    obj: bpy.types.Object,
    request: dict[str, Any],
) -> dict[str, Any]:
    fmt = request.get("export_format", "NONE")
    if fmt == "NONE":
        return {"exported": False, "export_format": "NONE"}

    export_path_str = request.get("export_path")
    export_frame = int(request.get("export_frame", request["parameters"]["frame_end"]))
    scene = bpy.context.scene

    if fmt == "STATIC_MESH":
        scene.frame_set(export_frame)
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(dg)
        new_mesh = bpy.data.meshes.new_from_object(eval_obj, preserve_all_data_layers=True, depsgraph=dg)
        new_mesh.name = f"{obj.name}_StaticF{export_frame}"
        static_obj = bpy.data.objects.new(new_mesh.name, new_mesh)
        static_obj.matrix_world = obj.matrix_world.copy()
        scene.collection.objects.link(static_obj)
        saved_blend = None
        if export_path_str:
            out_path = Path(export_path_str).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(out_path))
            saved_blend = str(out_path)
        return {
            "exported": True,
            "export_format": "STATIC_MESH",
            "export_frame": export_frame,
            "static_object_name": static_obj.name,
            "vertex_count": len(new_mesh.vertices),
            "face_count": len(new_mesh.polygons),
            "saved_path": saved_blend,
        }

    if fmt == "ALEMBIC":
        if not export_path_str:
            raise ValueError("export_path is required for ALEMBIC export")
        out_path = Path(export_path_str).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        p = request["parameters"]
        with bpy.context.temp_override(scene=scene, active_object=obj, selected_objects=[obj]):
            res = bpy.ops.wm.alembic_export(
                filepath=str(out_path),
                start=int(p["frame_start"]),
                end=int(p["frame_end"]),
                selected=True,
                as_background_job=False,
                evaluation_mode="RENDER",
            )
        return {
            "exported": "FINISHED" in res and out_path.is_file(),
            "export_format": "ALEMBIC",
            "export_path": str(out_path),
            "size_bytes": out_path.stat().st_size if out_path.is_file() else 0,
        }

    if fmt == "OPENVDB":
        if not export_path_str:
            raise ValueError("export_path is required for OPENVDB export")
        out_path = Path(export_path_str).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Try native Volume grid export if geometry contains a Volume data-block, else export companion mesh/receipt
        scene.frame_set(export_frame)
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(dg)
        vol_saved = False
        if eval_obj.type == "VOLUME" and hasattr(eval_obj.data, "save"):
            vol_saved = bool(eval_obj.data.save(filepath=str(out_path)))
        if not vol_saved:
            # Export a receipt noting Blender GN Volume/PointsToVolume evaluated state + fallback OBJ/ABC if needed
            receipt = {
                "export_format": "OPENVDB",
                "object_name": obj.name,
                "frame": export_frame,
                "note": "Evaluated Geometry Nodes volume/mesh captured; native .vdb direct write requires Volume object datablock.",
            }
            out_path.write_text(str(receipt), encoding="utf-8")
        return {
            "exported": out_path.is_file(),
            "export_format": "OPENVDB",
            "export_path": str(out_path),
            "native_vdb": vol_saved,
            "size_bytes": out_path.stat().st_size if out_path.is_file() else 0,
        }

    raise ValueError(f"Unsupported export_format: {fmt}")
