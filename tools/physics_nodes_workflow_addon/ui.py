"""Blender 3D Viewport N-Panel Workbench & Operators for Physics Nodes Workflow."""

from __future__ import annotations

import json
from typing import Any

import bpy
from bpy.props import (
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import contract as pnw_contract


def _preset_items(self, context):
    return [
        (pid, info["title"], info["description"])
        for pid, info in pnw_contract.PRESET_DEFINITIONS.items()
    ]


class PNW_ObjectSettings(bpy.types.PropertyGroup):
    preset_id: EnumProperty(
        name="Preset",
        description="Physics domain preset",
        items=_preset_items,
    )
    bridge_mode: EnumProperty(
        name="Solver Mode",
        description="Geometry Nodes native kernel or explicit bridge to Blender physics",
        items=[
            ("GN_NATIVE", "GN Native (Modular)", "Modular Geometry Nodes Simulation + Repeat substeps"),
            ("XPBD_ASSET_BRIDGE", "XPBD Asset Bridge", "Bridge to Blender 5.2 Cloth Dynamics (Experimental)"),
            ("BULLET_BRIDGE", "Bullet Rigid Body Bridge", "Bridge to native RigidBodyWorld"),
            ("MANTAFLOW_BRIDGE", "Mantaflow Fluid Bridge", "Bridge to native Fluid simulation modifier"),
        ],
        default="GN_NATIVE",
    )
    modifier_name: StringProperty(name="Modifier Name", default="PhysicsNodes")
    collider_object: PointerProperty(name="Collider", type=bpy.types.Object)
    instance_collection: PointerProperty(name="Instances", type=bpy.types.Collection)

    substeps: IntProperty(name="Substeps", default=4, min=1, max=8)
    seed: IntProperty(name="Seed", default=42, min=0)
    frame_start: IntProperty(name="Frame Start", default=1, min=1)
    frame_end: IntProperty(name="Frame End", default=120, min=1)

    gravity: FloatVectorProperty(name="Gravity", default=(0.0, 0.0, -9.81), size=3)
    drag: FloatProperty(name="Drag", default=0.04, min=0.0, max=20.0)
    restitution: FloatProperty(name="Restitution", default=0.35, min=0.0, max=1.0)
    friction: FloatProperty(name="Friction", default=0.45, min=0.0, max=1.0)
    sleep_velocity: FloatProperty(name="Sleep Velocity", default=0.05, min=0.0, max=10.0)
    contact_thickness: FloatProperty(name="Contact Thickness", default=0.08, min=0.001, max=2.0)
    particle_radius: FloatProperty(name="Particle Radius", default=0.12, min=0.001, max=5.0)
    ground_z: FloatProperty(name="Ground Z", default=0.0)

    scatter_count: IntProperty(name="Scatter Count", default=48, min=0, max=10000)
    angular_spin: FloatProperty(name="Angular Spin", default=1.2, min=0.0, max=50.0)
    stiffness: FloatProperty(name="Stiffness", default=0.7, min=0.0, max=1.0)
    anchor_pos: FloatVectorProperty(name="Anchor", default=(0.0, 0.0, 2.5), size=3)
    rest_length: FloatProperty(name="Rest Length", default=0.0, min=0.0)

    wind_vector: FloatVectorProperty(name="Wind", default=(0.0, 0.0, 0.0), size=3)
    wind_turbulence: FloatProperty(name="Turbulence", default=0.0, min=0.0, max=50.0)

    emit_rate: IntProperty(name="Emit Rate", default=24, min=0, max=5000)
    emit_speed: FloatProperty(name="Emit Speed", default=6.0, min=0.0, max=100.0)
    emit_end_frame: IntProperty(name="Emit End Frame", default=90, min=1)
    lifetime: IntProperty(name="Lifetime (Frames)", default=110, min=1)
    sdf_voxel_size: FloatProperty(name="SDF Voxel Size", default=0.08, min=0.01, max=2.0)

    bake_directory: StringProperty(name="Bake Directory", subtype="DIR_PATH", default="")
    last_inspect_summary: StringProperty(name="Last Inspection", default="")

    def to_request(self, obj: bpy.types.Object, operation: str, bake: bool = False) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "operation": operation,
            "preset_id": self.preset_id,
            "bridge_mode": self.bridge_mode,
            "target_object": obj.name,
            "modifier_name": self.modifier_name or "PhysicsNodes",
            "collider_object": self.collider_object.name if self.collider_object else None,
            "instance_collection": self.instance_collection.name if self.instance_collection else None,
            "bake": bake,
            "bake_directory": self.bake_directory or None,
            "parameters": {
                "substeps": int(self.substeps),
                "seed": int(self.seed),
                "frame_start": int(self.frame_start),
                "frame_end": int(self.frame_end),
                "gravity": list(self.gravity),
                "drag": float(self.drag),
                "restitution": float(self.restitution),
                "friction": float(self.friction),
                "sleep_velocity": float(self.sleep_velocity),
                "contact_thickness": float(self.contact_thickness),
                "particle_radius": float(self.particle_radius),
                "ground_z": float(self.ground_z),
                "scatter_count": int(self.scatter_count),
                "angular_spin": float(self.angular_spin),
                "stiffness": float(self.stiffness),
                "anchor_pos": list(self.anchor_pos),
                "rest_length": float(self.rest_length),
                "wind_vector": list(self.wind_vector),
                "wind_turbulence": float(self.wind_turbulence),
                "emit_rate": int(self.emit_rate),
                "emit_speed": float(self.emit_speed),
                "emit_end_frame": int(self.emit_end_frame),
                "lifetime": int(self.lifetime),
                "sdf_voxel_size": float(self.sdf_voxel_size),
            },
        }


def _run_op(operator, context, operation: str, bake: bool = False):
    from . import execute_request

    obj = context.active_object
    if obj is None:
        operator.report({"ERROR"}, "Select an active target object first")
        return {"CANCELLED"}
    req = obj.pnw_settings.to_request(obj, operation, bake=bake)
    res = execute_request(req)
    if not res.get("ok"):
        operator.report({"ERROR"}, res.get("error", {}).get("message", "Execution failed"))
        return {"CANCELLED"}
    if "inspection" in res and res["inspection"]:
        insp = res["inspection"]
        summary = (
            f"disp={insp.get('centroid_displacement')} "
            f"rest={insp.get('resting_ratio')} "
            f"jitter={insp.get('late_frame_jitter')} "
            f"baked={insp.get('cache_status', {}).get('is_baked')}"
        )
        obj.pnw_settings.last_inspect_summary = summary
    operator.report({"INFO"}, f"Physics Nodes [{operation}] finished")
    return {"FINISHED"}


class PNW_OT_load_preset_defaults(bpy.types.Operator):
    bl_idname = "pnw.load_preset_defaults"
    bl_label = "Load Preset Defaults"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            return {"CANCELLED"}
        s = obj.pnw_settings
        defaults = pnw_contract.PRESET_DEFINITIONS[s.preset_id]["defaults"]
        for k, v in defaults.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return {"FINISHED"}


class PNW_OT_build_workflow(bpy.types.Operator):
    bl_idname = "pnw.build_workflow"
    bl_label = "Build / Update Physics Nodes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _run_op(self, context, "build")


class PNW_OT_update_parameters(bpy.types.Operator):
    bl_idname = "pnw.update_parameters"
    bl_label = "Apply Parameters (Keep Edits)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _run_op(self, context, "parameters")


class PNW_OT_bake_cache(bpy.types.Operator):
    bl_idname = "pnw.bake_cache"
    bl_label = "Bake to Disk"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "bake", bake=True)


class PNW_OT_invalidate_cache(bpy.types.Operator):
    bl_idname = "pnw.invalidate_cache"
    bl_label = "Invalidate Cache"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "invalidate_cache")


class PNW_OT_inspect_workflow(bpy.types.Operator):
    bl_idname = "pnw.inspect_workflow"
    bl_label = "Inspect Simulation"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "inspect")


class PNW_PT_workbench(bpy.types.Panel):
    bl_label = "Physics Nodes Workbench"
    bl_idname = "VIEW_3D_PT_physics_nodes_workbench"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Physics Nodes"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None:
            layout.label(text="Select a target object", icon="INFO")
            return

        s = obj.pnw_settings
        box = layout.box()
        box.label(text="Domain & Solver", icon="PHYSICS")
        row = box.row(align=True)
        row.prop(s, "preset_id", text="")
        row.operator("pnw.load_preset_defaults", text="", icon="FILE_REFRESH")
        box.prop(s, "bridge_mode")
        box.prop(s, "modifier_name")
        box.prop(s, "collider_object")
        if pnw_contract.PRESET_DEFINITIONS[s.preset_id]["domain"] == "rigid_scatter":
            box.prop(s, "instance_collection")

        box_sim = layout.box()
        box_sim.label(text="Time & Determinism", icon="TIME")
        row = box_sim.row(align=True)
        row.prop(s, "frame_start")
        row.prop(s, "frame_end")
        row = box_sim.row(align=True)
        row.prop(s, "substeps")
        row.prop(s, "seed")

        box_phys = layout.box()
        box_phys.label(text="Forces & Contact", icon="FORCE_WIND")
        box_phys.prop(s, "gravity")
        box_phys.prop(s, "drag")
        box_phys.prop(s, "restitution")
        box_phys.prop(s, "friction")
        box_phys.prop(s, "sleep_velocity")
        box_phys.prop(s, "contact_thickness")
        box_phys.prop(s, "particle_radius")
        box_phys.prop(s, "ground_z")
        box_phys.prop(s, "wind_vector")
        box_phys.prop(s, "wind_turbulence")

        dom = pnw_contract.PRESET_DEFINITIONS[s.preset_id]["domain"]
        if dom == "rigid_scatter":
            box_d = layout.box()
            box_d.label(text="Rigid Scatter & Tether", icon="RIGID_BODY")
            box_d.prop(s, "scatter_count")
            box_d.prop(s, "angular_spin")
            box_d.prop(s, "stiffness")
            box_d.prop(s, "rest_length")
            box_d.prop(s, "anchor_pos")
        elif dom == "cloth_rope":
            box_d = layout.box()
            box_d.label(text="Cloth & Rope Elasticity", icon="MOD_CLOTH")
            box_d.prop(s, "stiffness")
        else:
            box_d = layout.box()
            box_d.label(text="Fluid & Gas Emission", icon="MOD_FLUIDSIM")
            box_d.prop(s, "emit_rate")
            box_d.prop(s, "emit_speed")
            box_d.prop(s, "emit_end_frame")
            box_d.prop(s, "lifetime")
            box_d.prop(s, "sdf_voxel_size")

        box_act = layout.box()
        box_act.label(text="Workflow Actions", icon="PLAY")
        box_act.prop(s, "bake_directory")
        box_act.operator("pnw.build_workflow", icon="NODETREE")
        box_act.operator("pnw.update_parameters", icon="CHECKMARK")
        row = box_act.row(align=True)
        row.operator("pnw.bake_cache", icon="DISK_DRIVE")
        row.operator("pnw.invalidate_cache", icon="TRASH")
        box_act.operator("pnw.inspect_workflow", icon="VIEWZOOM")
        if s.last_inspect_summary:
            box_act.label(text=s.last_inspect_summary)


CLASSES = (
    PNW_ObjectSettings,
    PNW_OT_load_preset_defaults,
    PNW_OT_build_workflow,
    PNW_OT_update_parameters,
    PNW_OT_bake_cache,
    PNW_OT_invalidate_cache,
    PNW_OT_inspect_workflow,
    PNW_PT_workbench,
)


def register_ui():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.pnw_settings = PointerProperty(type=PNW_ObjectSettings)


def unregister_ui():
    if hasattr(bpy.types.Object, "pnw_settings"):
        del bpy.types.Object.pnw_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
