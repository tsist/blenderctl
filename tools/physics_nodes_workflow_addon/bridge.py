"""Explicit bridges from Geometry Nodes Physics Workbench to Blender native physics systems."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy


def _builtin_dynamics_asset_blend() -> Path:
    datafiles = Path(bpy.utils.resource_path("LOCAL")) / "datafiles" / "assets" / "nodes" / "geometry_nodes_dynamics_assets.blend"
    return datafiles


def apply_bridge(
    obj: bpy.types.Object,
    request: dict[str, Any],
    collider_obj: bpy.types.Object | None = None,
) -> dict[str, Any]:
    mode = request["bridge_mode"]
    domain = request["domain"]
    params = request["parameters"]

    if mode == "GN_NATIVE":
        return {"bridge_mode": mode, "applied": False, "details": "Pure Geometry Nodes modular kernel active"}

    if mode == "XPBD_ASSET_BRIDGE":
        blend_file = _builtin_dynamics_asset_blend()
        if not blend_file.is_file():
            raise RuntimeError(f"Built-in dynamics asset library missing: {blend_file}")
        wanted = ["Cloth Dynamics (Experimental)", "Collider"]
        imported = []
        with bpy.data.libraries.load(str(blend_file), link=False) as (data_from, data_to):
            available = set(data_from.node_groups)
            targets = [n for n in wanted if n in available and n not in bpy.data.node_groups]
            data_to.node_groups = targets
            imported = list(targets)
        cloth_ng = bpy.data.node_groups.get("Cloth Dynamics (Experimental)")
        if cloth_ng is not None and obj.modifiers.get(request["modifier_name"] + "_XPBD") is None:
            mod = obj.modifiers.new(request["modifier_name"] + "_XPBD", "NODES")
            mod.node_group = cloth_ng
        return {
            "bridge_mode": mode,
            "applied": True,
            "asset_blend": str(blend_file),
            "imported_groups": imported,
            "available_cloth_group": cloth_ng is not None,
        }

    if mode == "BULLET_BRIDGE":
        scene = bpy.context.scene
        if scene.rigidbody_world is None:
            with bpy.context.temp_override(scene=scene):
                bpy.ops.rigidbody.world_add()
        rbw = scene.rigidbody_world
        rbw.substeps_per_frame = max(1, min(10, params["substeps"] * 2))
        if obj.rigid_body is None:
            with bpy.context.temp_override(object=obj, active_object=obj, selected_editable_objects=[obj]):
                bpy.ops.rigidbody.object_add(type="ACTIVE")
        obj.rigid_body.restitution = params["restitution"]
        obj.rigid_body.friction = params["friction"]
        if collider_obj is not None and collider_obj.rigid_body is None:
            with bpy.context.temp_override(object=collider_obj, active_object=collider_obj, selected_editable_objects=[collider_obj]):
                bpy.ops.rigidbody.object_add(type="PASSIVE")
            collider_obj.rigid_body.collision_shape = "MESH"
            collider_obj.rigid_body.restitution = params["restitution"]
            collider_obj.rigid_body.friction = params["friction"]
        return {
            "bridge_mode": mode,
            "applied": True,
            "domain": domain,
            "rigidbody_substeps": rbw.substeps_per_frame,
        }

    if mode == "MANTAFLOW_BRIDGE":
        mod_name = request["modifier_name"] + "_FluidFlow"
        mod = obj.modifiers.get(mod_name) or obj.modifiers.new(mod_name, "FLUID")
        mod.fluid_type = "FLOW"
        mod.flow_settings.flow_type = "LIQUID" if "fluid" in request["preset_id"] else "SMOKE"
        mod.flow_settings.flow_behavior = "INFLOW"
        if collider_obj is not None:
            cmod = collider_obj.modifiers.get("PNW_FluidEffector") or collider_obj.modifiers.new("PNW_FluidEffector", "FLUID")
            cmod.fluid_type = "EFFECTOR"
            cmod.effector_settings.effector_type = "COLLISION"
        return {
            "bridge_mode": mode,
            "applied": True,
            "flow_type": mod.flow_settings.flow_type,
        }

    raise RuntimeError(f"Unsupported bridge_mode: {mode}")
