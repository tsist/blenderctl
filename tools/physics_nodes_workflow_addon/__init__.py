"""Blender 5.2.1 LTS Physics Nodes Workflow Addon.

Provides modular Geometry Nodes physics node group assets, an interactive N-panel
workbench, disk bake cache hashing/invalidation, and a shared deterministic
request execution entrypoint for physics_workflow_cli_v1.
"""

from __future__ import annotations

import traceback
from typing import Any

from . import contract as pnw_contract

try:
    import bpy
except ImportError:  # Allow pure-Python CLI import of physics_nodes_workflow_addon.contract
    bpy = None  # type: ignore[assignment]

if bpy is not None:
    from . import bake as pnw_bake
    from . import core as pnw_core
    from . import export as pnw_export
    from . import inspection as pnw_inspection
    from . import subgroups as pnw_subgroups
    from . import ui as pnw_ui


bl_info = {
    "name": "Physics Nodes Workbench",
    "author": "Blender AI Pipeline",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "View3D > UI > Physics Nodes",
    "description": "Modular Geometry Nodes Physics Node Library, Workbench, Disk Cache & CLI",
    "category": "Physics",
}


def execute_request(raw_request: dict[str, Any]) -> dict[str, Any]:
    """Unified deterministic execution entrypoint shared by UI operators and CLI workers."""
    try:
        req = pnw_contract.normalize_request(raw_request)
    except pnw_contract.PhysicsContractError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    target_name = req["target_object"]
    obj = bpy.data.objects.get(target_name) if target_name else bpy.context.active_object
    if obj is None:
        return {
            "ok": False,
            "error": {"code": "SCENE_STATE_ERROR", "message": f"Target object not found: '{target_name}'"},
        }

    op = req["operation"]
    mod_name = req["modifier_name"]
    p = req["parameters"]

    try:
        result: dict[str, Any] = {
            "ok": True,
            "contract_id": req["contract_id"],
            "operation": op,
            "domain": req["domain"],
            "preset_id": req["preset_id"],
            "bridge_mode": req["bridge_mode"],
            "target_object": obj.name,
            "modifier_name": mod_name,
        }

        if op in ("build", "update", "parameters", "adopt"):
            build_info = pnw_core.build_or_update_workflow(obj, req)
            result["workflow"] = build_info

            mod = obj.modifiers.get(mod_name)
            if req["bake"] and mod is not None:
                bake_info = pnw_bake.execute_disk_bake(
                    obj=obj,
                    mod=mod,
                    frame_start=p["frame_start"],
                    frame_end=p["frame_end"],
                    params_hash=req["parameters_hash"],
                    graph_fingerprint=build_info["graph_fingerprint"],
                    requested_dir=req.get("bake_directory"),
                )
                result["bake"] = bake_info

            result["inspection"] = pnw_inspection.inspect_workflow(
                obj=obj,
                mod=mod,
                frame_start=p["frame_start"],
                frame_end=p["frame_end"],
                ground_z=p["ground_z"],
                sleep_velocity=p["sleep_velocity"],
                params_hash=req["parameters_hash"],
            )

        elif op == "bake":
            mod = obj.modifiers.get(mod_name)
            if mod is None or mod.type != "NODES" or mod.node_group is None:
                raise ValueError(f"Physics modifier '{mod_name}' not found on '{obj.name}'")
            wrapper = mod.node_group
            core_node = wrapper.nodes.get("PNW_CoreNode") or next(
                (n for n in wrapper.nodes if n.bl_idname == "GeometryNodeGroup"), None
            )
            graph_fp = pnw_subgroups.tree_fingerprint(core_node.node_tree if core_node else wrapper)
            result["bake"] = pnw_bake.execute_disk_bake(
                obj=obj,
                mod=mod,
                frame_start=p["frame_start"],
                frame_end=p["frame_end"],
                params_hash=req["parameters_hash"],
                graph_fingerprint=graph_fp,
                requested_dir=req.get("bake_directory"),
            )
            result["inspection"] = pnw_inspection.inspect_workflow(
                obj=obj,
                mod=mod,
                frame_start=p["frame_start"],
                frame_end=p["frame_end"],
                ground_z=p["ground_z"],
                sleep_velocity=p["sleep_velocity"],
                params_hash=req["parameters_hash"],
            )

        elif op == "invalidate_cache":
            mod = obj.modifiers.get(mod_name)
            if mod is None:
                raise ValueError(f"Physics modifier '{mod_name}' not found on '{obj.name}'")
            result["invalidate"] = pnw_bake.invalidate_cache(obj, mod, req.get("bake_directory"))

        elif op == "inspect":
            mod = obj.modifiers.get(mod_name)
            result["inspection"] = pnw_inspection.inspect_workflow(
                obj=obj,
                mod=mod,
                frame_start=p["frame_start"],
                frame_end=p["frame_end"],
                ground_z=p["ground_z"],
                sleep_velocity=p["sleep_velocity"],
                params_hash=req["parameters_hash"],
            )

        elif op == "export":
            result["export"] = pnw_export.export_workflow_result(obj, req)

        # If export_format requested alongside build/bake, execute export as well
        if op != "export" and req.get("export_format", "NONE") != "NONE":
            result["export"] = pnw_export.export_workflow_result(obj, req)

        return result

    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "EXECUTION_ERROR",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }


def register():
    pnw_ui.register_ui()


def unregister():
    pnw_ui.unregister_ui()
