"""Core Geometry Node tree assembly (PNW_Wrapper + PNW_Core), parameter updates, and manual-edit protection."""

from __future__ import annotations

import json
from typing import Any

import bpy

from . import bake as pnw_bake
from . import bridge as pnw_bridge
from . import subgroups as pnw_subgroups


ROLE_KEY = "pnw_role"
STATE_PROP = "pnw_state_json"
FINGERPRINT_KEY = "pnw_last_fingerprint"

# Public socket definitions shared by Core and Wrapper node groups.
STANDARD_INPUT_SOCKETS: tuple[tuple[str, str, Any], ...] = (
    ("Geometry", "NodeSocketGeometry", None),
    ("Substeps", "NodeSocketInt", 4),
    ("Seed", "NodeSocketInt", 42),
    ("Gravity", "NodeSocketVector", (0.0, 0.0, -9.81)),
    ("Drag", "NodeSocketFloat", 0.04),
    ("Wind Vector", "NodeSocketVector", (0.0, 0.0, 0.0)),
    ("Wind Turbulence", "NodeSocketFloat", 0.0),
    ("Stiffness", "NodeSocketFloat", 0.7),
    ("Restitution", "NodeSocketFloat", 0.35),
    ("Friction", "NodeSocketFloat", 0.45),
    ("Sleep Velocity", "NodeSocketFloat", 0.05),
    ("Contact Thickness", "NodeSocketFloat", 0.08),
    ("Particle Radius", "NodeSocketFloat", 0.12),
    ("Ground Z", "NodeSocketFloat", 0.0),
    ("Scatter Count", "NodeSocketInt", 48),
    ("Angular Spin", "NodeSocketFloat", 1.2),
    ("Anchor", "NodeSocketVector", (0.0, 0.0, 2.5)),
    ("Rest Length", "NodeSocketFloat", 0.0),
    ("Emit Rate", "NodeSocketInt", 24),
    ("Emit Speed", "NodeSocketFloat", 6.0),
    ("Emit End Frame", "NodeSocketInt", 90),
    ("Lifetime", "NodeSocketInt", 120),
    ("SDF Voxel Size", "NodeSocketFloat", 0.08),
    ("Collider", "NodeSocketObject", None),
    ("Instance Collection", "NodeSocketCollection", None),
)


def _socket_value_from_request(request: dict[str, Any], socket_name: str, obj: bpy.types.Object) -> Any:
    p = request["parameters"]
    mapping: dict[str, Any] = {
        "Substeps": int(p["substeps"]),
        "Seed": int(p["seed"]),
        "Gravity": tuple(float(x) for x in p["gravity"]),
        "Drag": float(p["drag"]),
        "Wind Vector": tuple(float(x) for x in p["wind_vector"]),
        "Wind Turbulence": float(p["wind_turbulence"]),
        "Stiffness": float(p["stiffness"]),
        "Restitution": float(p["restitution"]),
        "Friction": float(p["friction"]),
        "Sleep Velocity": float(p["sleep_velocity"]),
        "Contact Thickness": float(p["contact_thickness"]),
        "Particle Radius": float(p["particle_radius"]),
        "Ground Z": float(p["ground_z"]),
        "Scatter Count": int(p["scatter_count"]),
        "Angular Spin": float(p["angular_spin"]),
        "Anchor": tuple(float(x) for x in p["anchor_pos"]),
        "Rest Length": float(p["rest_length"]),
        "Emit Rate": int(p["emit_rate"]),
        "Emit Speed": float(p["emit_speed"]),
        "Emit End Frame": int(p["emit_end_frame"]),
        "Lifetime": int(p["lifetime"]),
        "SDF Voxel Size": float(p["sdf_voxel_size"]),
    }
    if socket_name == "Collider":
        cname = request.get("collider_object")
        if not cname:
            return None
        col_obj = bpy.data.objects.get(cname)
        if col_obj is None:
            raise ValueError(f"Collider object not found: {cname}")
        if col_obj == obj:
            raise ValueError("Collider object cannot reference target object itself")
        return col_obj
    if socket_name == "Instance Collection":
        coll_name = request.get("instance_collection")
        if not coll_name:
            return None
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            raise ValueError(f"Instance collection not found: {coll_name}")
        return coll
    return mapping.get(socket_name)


def _set_modifier_socket(mod: bpy.types.Modifier, identifier: str, value: Any) -> None:
    try:
        sock_prop = getattr(mod.properties.inputs, identifier)
    except AttributeError:
        sock_prop = mod.properties.inputs[identifier]
    if hasattr(sock_prop, "value"):
        sock_prop.value = value
    else:
        sock_prop["value"] = value


def apply_request_parameters(obj: bpy.types.Object, mod: bpy.types.Modifier, request: dict[str, Any]) -> None:
    tree = mod.node_group
    if tree is None:
        raise ValueError("Modifier has no node_group assigned")
    for item in tree.interface.items_tree:
        if item.item_type != "SOCKET" or item.in_out != "INPUT" or item.socket_type == "NodeSocketGeometry":
            continue
        if item.name in {s[0] for s in STANDARD_INPUT_SOCKETS}:
            val = _socket_value_from_request(request, item.name, obj)
            _set_modifier_socket(mod, item.identifier, val)
    tree.interface_update(bpy.context)
    obj.update_tag()


def _surface_area_hint(obj: bpy.types.Object) -> float:
    if obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0:
        area = sum(p.area for p in obj.data.polygons)
        return max(float(area), 0.05)
    return 1.0


def _ensure_standard_interface(tree: bpy.types.NodeTree) -> None:
    existing_in = {
        item.name
        for item in tree.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "INPUT"
    }
    for name, sock_type, default_val in STANDARD_INPUT_SOCKETS:
        if name not in existing_in:
            pnw_subgroups.add_socket(tree, name, "INPUT", sock_type, default_val)
    existing_out = {
        item.name
        for item in tree.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "OUTPUT"
    }
    if "Geometry" not in existing_out:
        pnw_subgroups.add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")


def _build_core_tree(
    core_name: str,
    obj: bpy.types.Object,
    request: dict[str, Any],
    subgroups: dict[str, bpy.types.NodeTree],
    force_rebuild: bool = False,
) -> bpy.types.NodeTree:
    domain = request["domain"]
    preset_id = request["preset_id"]

    existing = bpy.data.node_groups.get(core_name)
    if existing is not None:
        existing.nodes.clear()
        if force_rebuild:
            existing.interface.clear()
        tree = existing
    else:
        tree = bpy.data.node_groups.new(core_name, "GeometryNodeTree")

    tree[ROLE_KEY] = "core"
    _ensure_standard_interface(tree)

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1600, 0)
    gout.location = (1800, 0)

    # Pre-simulation setup per domain
    area_hint = _surface_area_hint(obj)

    if domain == "rigid_scatter":
        # Convert mesh faces -> distributed points if Scatter Count > 0 and mesh has faces, else mesh->points
        dom_sz = tree.nodes.new("GeometryNodeAttributeDomainSize")
        dom_sz.component = "MESH"
        dom_sz.location = (-1350, 220)
        tree.links.new(gin.outputs["Geometry"], dom_sz.inputs["Geometry"])

        has_faces = tree.nodes.new("ShaderNodeMath")
        has_faces.operation = "GREATER_THAN"
        has_faces.inputs[1].default_value = 0.0
        has_faces.location = (-1160, 240)
        tree.links.new(dom_sz.outputs["Face Count"], has_faces.inputs[0])

        want_scatter = tree.nodes.new("ShaderNodeMath")
        want_scatter.operation = "GREATER_THAN"
        want_scatter.inputs[1].default_value = 0.0
        want_scatter.location = (-1160, 100)
        tree.links.new(gin.outputs["Scatter Count"], want_scatter.inputs[0])

        use_dist = tree.nodes.new("ShaderNodeMath")
        use_dist.operation = "MULTIPLY"
        use_dist.location = (-980, 180)
        tree.links.new(has_faces.outputs["Value"], use_dist.inputs[0])
        tree.links.new(want_scatter.outputs["Value"], use_dist.inputs[1])

        dens = tree.nodes.new("ShaderNodeMath")
        dens.operation = "DIVIDE"
        dens.inputs[1].default_value = area_hint
        dens.location = (-1160, -40)
        tree.links.new(gin.outputs["Scatter Count"], dens.inputs[0])

        dist = tree.nodes.new("GeometryNodeDistributePointsOnFaces")
        dist.distribute_method = "RANDOM"
        dist.location = (-980, -20)
        tree.links.new(gin.outputs["Geometry"], dist.inputs["Mesh"])
        tree.links.new(dens.outputs["Value"], dist.inputs["Density"])
        tree.links.new(gin.outputs["Seed"], dist.inputs["Seed"])

        m2p = tree.nodes.new("GeometryNodeMeshToPoints")
        m2p.location = (-980, -220)
        tree.links.new(gin.outputs["Geometry"], m2p.inputs["Mesh"])

        sw_pts = tree.nodes.new("GeometryNodeSwitch")
        sw_pts.input_type = "GEOMETRY"
        sw_pts.location = (-780, 20)
        tree.links.new(use_dist.outputs["Value"], sw_pts.inputs["Switch"])
        tree.links.new(m2p.outputs["Points"], sw_pts.inputs["False"])
        tree.links.new(dist.outputs["Points"], sw_pts.inputs["True"])

        # Seeded slight initial velocity jitter + random rotation
        rv_rot = tree.nodes.new("FunctionNodeRandomValue")
        rv_rot.data_type = "FLOAT_VECTOR"
        rv_rot.inputs["Min"].default_value = (-1.5, -1.5, -1.5)
        rv_rot.inputs["Max"].default_value = (1.5, 1.5, 1.5)
        rv_rot.location = (-780, -200)
        tree.links.new(gin.outputs["Seed"], rv_rot.inputs["Seed"])

        st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_vel.data_type = "FLOAT_VECTOR"
        st_vel.domain = "POINT"
        st_vel.inputs["Name"].default_value = "pnw_velocity"
        st_vel.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_vel.location = (-580, 20)
        tree.links.new(sw_pts.outputs["Output"], st_vel.inputs["Geometry"])

        st_rot = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_rot.data_type = "FLOAT_VECTOR"
        st_rot.domain = "POINT"
        st_rot.inputs["Name"].default_value = "pnw_rotation"
        st_rot.location = (-400, 20)
        tree.links.new(st_vel.outputs["Geometry"], st_rot.inputs["Geometry"])
        tree.links.new(rv_rot.outputs["Value"], st_rot.inputs["Value"])
        pre_sim_geo = st_rot.outputs["Geometry"]

    elif domain == "cloth_rope":
        # Convert Curve to Mesh edges automatically if object is a curve; leave Mesh untouched
        is_curve_src = (obj.type == "CURVE")
        if is_curve_src:
            c2m = tree.nodes.new("GeometryNodeCurveToMesh")
            c2m.location = (-1250, 20)
            tree.links.new(gin.outputs["Geometry"], c2m.inputs["Curve"])
            base_geo_out = c2m.outputs["Mesh"]
        else:
            base_geo_out = gin.outputs["Geometry"]

        pos_pre = tree.nodes.new("GeometryNodeInputPosition")
        pos_pre.location = (-1250, -160)

        st_rest = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_rest.data_type = "FLOAT_VECTOR"
        st_rest.domain = "POINT"
        st_rest.inputs["Name"].default_value = "pnw_rest_pos"
        st_rest.location = (-1040, 20)
        tree.links.new(base_geo_out, st_rest.inputs["Geometry"])
        tree.links.new(pos_pre.outputs["Position"], st_rest.inputs["Value"])

        st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_vel.data_type = "FLOAT_VECTOR"
        st_vel.domain = "POINT"
        st_vel.inputs["Name"].default_value = "pnw_velocity"
        st_vel.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_vel.location = (-840, 20)
        tree.links.new(st_rest.outputs["Geometry"], st_vel.inputs["Geometry"])

        # Read user-provided "pnw_pinned" attribute if present, else auto-pin top band (Z >= bbox_max.z - 0.08)
        pin_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
        pin_attr.data_type = "FLOAT"
        pin_attr.inputs["Name"].default_value = "pnw_pinned"
        pin_attr.location = (-1040, -280)

        bbox = tree.nodes.new("GeometryNodeBoundBox")
        bbox.location = (-1040, -440)
        tree.links.new(base_geo_out, bbox.inputs["Geometry"])

        sep_max = tree.nodes.new("ShaderNodeSeparateXYZ")
        sep_max.location = (-860, -440)
        tree.links.new(bbox.outputs["Max"], sep_max.inputs["Vector"])

        band_z = tree.nodes.new("ShaderNodeMath")
        band_z.operation = "SUBTRACT"
        band_z.inputs[1].default_value = 0.08
        band_z.location = (-680, -440)
        tree.links.new(sep_max.outputs["Z"], band_z.inputs[0])

        sep_p = tree.nodes.new("ShaderNodeSeparateXYZ")
        sep_p.location = (-860, -280)
        tree.links.new(pos_pre.outputs["Position"], sep_p.inputs["Vector"])

        auto_pin = tree.nodes.new("ShaderNodeMath")
        auto_pin.operation = "GREATER_THAN"
        auto_pin.location = (-520, -340)
        tree.links.new(sep_p.outputs["Z"], auto_pin.inputs[0])
        tree.links.new(band_z.outputs["Value"], auto_pin.inputs[1])

        sw_pin = tree.nodes.new("GeometryNodeSwitch")
        sw_pin.input_type = "FLOAT"
        sw_pin.location = (-520, -160)
        tree.links.new(pin_attr.outputs["Exists"], sw_pin.inputs["Switch"])
        tree.links.new(auto_pin.outputs["Value"], sw_pin.inputs["False"])
        tree.links.new(pin_attr.outputs["Attribute"], sw_pin.inputs["True"])

        st_pin = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_pin.data_type = "FLOAT"
        st_pin.domain = "POINT"
        st_pin.inputs["Name"].default_value = "pnw_pinned"
        st_pin.location = (-400, 20)
        tree.links.new(st_vel.outputs["Geometry"], st_pin.inputs["Geometry"])
        tree.links.new(sw_pin.outputs["Output"], st_pin.inputs["Value"])
        pre_sim_geo = st_pin.outputs["Geometry"]

    else:  # fluid_emission
        # Start with empty point cloud; Simulation Zone spawns particles each frame <= Emit End Frame
        m2p_init = tree.nodes.new("GeometryNodeMeshToPoints")
        m2p_init.location = (-820, 40)
        tree.links.new(gin.outputs["Geometry"], m2p_init.inputs["Mesh"])

        del_all = tree.nodes.new("GeometryNodeDeleteGeometry")
        del_all.inputs["Selection"].default_value = True
        del_all.location = (-640, 40)
        tree.links.new(m2p_init.outputs["Points"], del_all.inputs["Geometry"])

        st_v0 = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_v0.data_type = "FLOAT_VECTOR"
        st_v0.domain = "POINT"
        st_v0.inputs["Name"].default_value = "pnw_velocity"
        st_v0.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_v0.location = (-460, 40)
        tree.links.new(del_all.outputs["Geometry"], st_v0.inputs["Geometry"])

        st_a0 = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_a0.data_type = "FLOAT"
        st_a0.domain = "POINT"
        st_a0.inputs["Name"].default_value = "pnw_age"
        st_a0.inputs["Value"].default_value = 0.0
        st_a0.location = (-300, 40)
        tree.links.new(st_v0.outputs["Geometry"], st_a0.inputs["Geometry"])
        pre_sim_geo = st_a0.outputs["Geometry"]

    # Root Simulation Zone
    sim_in = tree.nodes.new("GeometryNodeSimulationInput")
    sim_out = tree.nodes.new("GeometryNodeSimulationOutput")
    sim_in.location = (-120, 0)
    sim_out.location = (1180, 0)
    sim_in.pair_with_output(sim_out)
    tree.links.new(pre_sim_geo, sim_in.inputs["Geometry"])

    scene_time = tree.nodes.new("GeometryNodeInputSceneTime")
    scene_time.location = (-120, -260)

    # For fluid_emission, emit new particles per frame inside Simulation Zone and cull expired ones
    if domain == "fluid_emission":
        can_emit = tree.nodes.new("ShaderNodeMath")
        can_emit.operation = "LESS_THAN"
        can_emit.location = (40, 320)
        tree.links.new(scene_time.outputs["Frame"], can_emit.inputs[0])
        # Frame <= Emit End Frame -> Frame < Emit End Frame + 1
        end_plus = tree.nodes.new("ShaderNodeMath")
        end_plus.operation = "ADD"
        end_plus.inputs[1].default_value = 1.0
        end_plus.location = (-120, 340)
        tree.links.new(gin.outputs["Emit End Frame"], end_plus.inputs[0])
        tree.links.new(end_plus.outputs["Value"], can_emit.inputs[1])

        emit_seed = tree.nodes.new("ShaderNodeMath")
        emit_seed.operation = "ADD"
        emit_seed.location = (40, 180)
        tree.links.new(gin.outputs["Seed"], emit_seed.inputs[0])
        tree.links.new(scene_time.outputs["Frame"], emit_seed.inputs[1])

        emit_dens = tree.nodes.new("ShaderNodeMath")
        emit_dens.operation = "DIVIDE"
        emit_dens.inputs[1].default_value = area_hint
        emit_dens.location = (-120, 200)
        tree.links.new(gin.outputs["Emit Rate"], emit_dens.inputs[0])

        dist_emit = tree.nodes.new("GeometryNodeDistributePointsOnFaces")
        dist_emit.distribute_method = "RANDOM"
        dist_emit.location = (220, 220)
        tree.links.new(gin.outputs["Geometry"], dist_emit.inputs["Mesh"])
        tree.links.new(emit_dens.outputs["Value"], dist_emit.inputs["Density"])
        tree.links.new(emit_seed.outputs["Value"], dist_emit.inputs["Seed"])

        # Seeded cone velocity around +Z * Emit Speed
        rv_spray = tree.nodes.new("FunctionNodeRandomValue")
        rv_spray.data_type = "FLOAT_VECTOR"
        rv_spray.inputs["Min"].default_value = (-1.2, -1.2, 0.7)
        rv_spray.inputs["Max"].default_value = (1.2, 1.2, 1.3)
        rv_spray.location = (220, 420)
        tree.links.new(emit_seed.outputs["Value"], rv_spray.inputs["Seed"])

        v_emit = tree.nodes.new("ShaderNodeVectorMath")
        v_emit.operation = "SCALE"
        v_emit.location = (400, 420)
        tree.links.new(rv_spray.outputs["Value"], v_emit.inputs[0])
        tree.links.new(gin.outputs["Emit Speed"], v_emit.inputs["Scale"])

        st_ev = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_ev.data_type = "FLOAT_VECTOR"
        st_ev.domain = "POINT"
        st_ev.inputs["Name"].default_value = "pnw_velocity"
        st_ev.location = (420, 220)
        tree.links.new(dist_emit.outputs["Points"], st_ev.inputs["Geometry"])
        tree.links.new(v_emit.outputs["Vector"], st_ev.inputs["Value"])

        st_ea = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_ea.data_type = "FLOAT"
        st_ea.domain = "POINT"
        st_ea.inputs["Name"].default_value = "pnw_age"
        st_ea.inputs["Value"].default_value = 0.0
        st_ea.location = (600, 220)
        tree.links.new(st_ev.outputs["Geometry"], st_ea.inputs["Geometry"])

        sw_emit = tree.nodes.new("GeometryNodeSwitch")
        sw_emit.input_type = "GEOMETRY"
        sw_emit.location = (760, 220)
        tree.links.new(can_emit.outputs["Value"], sw_emit.inputs["Switch"])
        tree.links.new(st_ea.outputs["Geometry"], sw_emit.inputs["True"])

        # Join existing simulated particles with newly spawned ones
        join_sim = tree.nodes.new("GeometryNodeJoinGeometry")
        join_sim.location = (920, 120)
        tree.links.new(sim_in.outputs["Geometry"], join_sim.inputs["Geometry"])
        tree.links.new(sw_emit.outputs["Output"], join_sim.inputs["Geometry"])

        # Delete particles older than Lifetime / 24.0 seconds
        age_read = tree.nodes.new("GeometryNodeInputNamedAttribute")
        age_read.data_type = "FLOAT"
        age_read.inputs["Name"].default_value = "pnw_age"
        age_read.location = (600, 420)

        life_sec = tree.nodes.new("ShaderNodeMath")
        life_sec.operation = "DIVIDE"
        life_sec.inputs[1].default_value = 24.0
        life_sec.location = (600, 560)
        tree.links.new(gin.outputs["Lifetime"], life_sec.inputs[0])

        expired = tree.nodes.new("ShaderNodeMath")
        expired.operation = "GREATER_THAN"
        expired.location = (780, 460)
        tree.links.new(age_read.outputs["Attribute"], expired.inputs[0])
        tree.links.new(life_sec.outputs["Value"], expired.inputs[1])

        cull_old = tree.nodes.new("GeometryNodeDeleteGeometry")
        cull_old.location = (920, 320)
        tree.links.new(join_sim.outputs["Geometry"], cull_old.inputs["Geometry"])
        tree.links.new(expired.outputs["Value"], cull_old.inputs["Selection"])
        sim_body_in_geo = cull_old.outputs["Geometry"]
    else:
        sim_body_in_geo = sim_in.outputs["Geometry"]

    # Inner Repeat Zone for Substeps
    rep_in = tree.nodes.new("GeometryNodeRepeatInput")
    rep_out = tree.nodes.new("GeometryNodeRepeatOutput")
    rep_in.location = (220, -40)
    rep_out.location = (920, -40)
    rep_in.pair_with_output(rep_out)
    tree.links.new(gin.outputs["Substeps"], rep_in.inputs["Iterations"])
    tree.links.new(sim_body_in_geo, rep_in.inputs["Geometry"])

    dt_sub = tree.nodes.new("ShaderNodeMath")
    dt_sub.operation = "DIVIDE"
    dt_sub.location = (180, -300)
    tree.links.new(sim_in.outputs["Delta Time"], dt_sub.inputs[0])
    tree.links.new(gin.outputs["Substeps"], dt_sub.inputs[1])

    # Convert world Ground Z into object-local Z so objects placed above world Z=0 fall all the way to the world ground
    self_obj = tree.nodes.new("GeometryNodeSelfObject")
    self_obj.location = (-20, -480)
    self_info = tree.nodes.new("GeometryNodeObjectInfo")
    self_info.transform_space = "ORIGINAL"
    self_info.location = (140, -480)
    tree.links.new(self_obj.outputs["Self Object"], self_info.inputs["Object"])

    sep_self = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_self.location = (300, -480)
    tree.links.new(self_info.outputs["Location"], sep_self.inputs["Vector"])

    local_ground_z = tree.nodes.new("ShaderNodeMath")
    local_ground_z.operation = "SUBTRACT"
    local_ground_z.location = (440, -480)
    tree.links.new(gin.outputs["Ground Z"], local_ground_z.inputs[0])
    tree.links.new(sep_self.outputs["Z"], local_ground_z.inputs[1])

    local_anchor = tree.nodes.new("ShaderNodeVectorMath")
    local_anchor.operation = "SUBTRACT"
    local_anchor.location = (440, -640)
    tree.links.new(gin.outputs["Anchor"], local_anchor.inputs[0])
    tree.links.new(self_info.outputs["Location"], local_anchor.inputs[1])

    # Call the domain solver subgroup inside Repeat Zone
    solver = tree.nodes.new("GeometryNodeGroup")
    solver.location = (560, -100)
    if domain == "rigid_scatter":
        solver.node_tree = subgroups["PN_Solver_RigidScatterStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Particle Radius"], solver.inputs["Radius"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])
        tree.links.new(gin.outputs["Sleep Velocity"], solver.inputs["Sleep Velocity"])
        tree.links.new(gin.outputs["Angular Spin"], solver.inputs["Angular Spin"])
        tree.links.new(local_anchor.outputs["Vector"], solver.inputs["Anchor"])
        tree.links.new(gin.outputs["Rest Length"], solver.inputs["Rest Length"])
        tree.links.new(gin.outputs["Stiffness"], solver.inputs["Stiffness"])
    elif domain == "cloth_rope":
        solver.node_tree = subgroups["PN_Solver_ClothRopeStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Stiffness"], solver.inputs["Stiffness"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])
        tree.links.new(gin.outputs["Sleep Velocity"], solver.inputs["Sleep Velocity"])
    else:  # fluid_emission
        solver.node_tree = subgroups["PN_Solver_FluidParticleStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])

    tree.links.new(solver.outputs["Geometry"], rep_out.inputs["Geometry"])
    tree.links.new(rep_out.outputs["Geometry"], sim_out.inputs["Geometry"])

    # Post-simulation stage outside Simulation Zone
    if domain == "rigid_scatter":
        post = tree.nodes.new("GeometryNodeGroup")
        post.node_tree = subgroups["PN_Post_InstanceRigid"]
        post.location = (1460, 0)
        tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
        tree.links.new(gin.outputs["Instance Collection"], post.inputs["Instance Collection"])
        tree.links.new(gin.outputs["Particle Radius"], post.inputs["Radius"])
        tree.links.new(post.outputs["Geometry"], gout.inputs["Geometry"])
    elif domain == "cloth_rope":
        if obj.type == "CURVE":
            # Convert simulated polyline edges back to smooth tubular rope mesh
            m2c = tree.nodes.new("GeometryNodeMeshToCurve")
            m2c.location = (1380, 0)
            tree.links.new(sim_out.outputs["Geometry"], m2c.inputs["Mesh"])

            prof = tree.nodes.new("GeometryNodeCurvePrimitiveCircle")
            prof.inputs["Resolution"].default_value = 8
            prof.location = (1380, -180)
            tree.links.new(gin.outputs["Particle Radius"], prof.inputs["Radius"])

            rc2m = tree.nodes.new("GeometryNodeCurveToMesh")
            rc2m.inputs["Fill Caps"].default_value = True
            rc2m.location = (1580, 0)
            tree.links.new(m2c.outputs["Curve"], rc2m.inputs["Curve"])
            tree.links.new(prof.outputs["Curve"], rc2m.inputs["Profile Curve"])
            post_out_sock = rc2m.outputs["Mesh"]
        else:
            smooth = tree.nodes.new("GeometryNodeSetShadeSmooth")
            smooth.location = (1460, 0)
            tree.links.new(sim_out.outputs["Geometry"], smooth.inputs[0])
            post_out_sock = smooth.outputs[0]

        if hasattr(obj.data, "materials") and len(obj.data.materials) > 0 and obj.data.materials[0] is not None:
            set_mat = tree.nodes.new("GeometryNodeSetMaterial")
            set_mat.location = (1680, 0)
            set_mat.inputs["Material"].default_value = obj.data.materials[0]
            tree.links.new(post_out_sock, set_mat.inputs["Geometry"])
            tree.links.new(set_mat.outputs["Geometry"], gout.inputs["Geometry"])
        else:
            tree.links.new(post_out_sock, gout.inputs["Geometry"])
    else:  # fluid_emission
        post = tree.nodes.new("GeometryNodeGroup")
        post.location = (1460, 0)
        if preset_id == "smoke_plume_volume":
            post.node_tree = subgroups["PN_Post_SmokeVolume"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Particle Radius"], post.inputs["Particle Radius"])
            tree.links.new(gin.outputs["SDF Voxel Size"], post.inputs["Voxel Size"])
            post_out_sock = post.outputs["Geometry"]
        else:
            post.node_tree = subgroups["PN_Post_FluidSDFMesh"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Particle Radius"], post.inputs["Particle Radius"])
            tree.links.new(gin.outputs["SDF Voxel Size"], post.inputs["Voxel Size"])
            post_out_sock = post.outputs["Mesh"]

        if hasattr(obj.data, "materials") and len(obj.data.materials) > 0 and obj.data.materials[0] is not None:
            set_mat = tree.nodes.new("GeometryNodeSetMaterial")
            set_mat.location = (1680, 0)
            set_mat.inputs["Material"].default_value = obj.data.materials[0]
            tree.links.new(post_out_sock, set_mat.inputs["Geometry"])
            tree.links.new(set_mat.outputs["Geometry"], gout.inputs["Geometry"])
        else:
            tree.links.new(post_out_sock, gout.inputs["Geometry"])

    tree[FINGERPRINT_KEY] = pnw_subgroups.tree_fingerprint(tree)
    return tree


def _build_or_preserve_wrapper(
    wrapper_name: str,
    core: bpy.types.NodeTree,
    force_rebuild: bool = False,
) -> tuple[bpy.types.NodeTree, bool]:
    """Create wrapper tree or safely rebind Core inside existing wrapper while preserving user edits."""
    existing = bpy.data.node_groups.get(wrapper_name)
    if existing is None or force_rebuild:
        if existing is not None:
            existing.nodes.clear()
            existing.interface.clear()
            wrapper = existing
        else:
            wrapper = bpy.data.node_groups.new(wrapper_name, "GeometryNodeTree")
        wrapper[ROLE_KEY] = "wrapper"
        _ensure_standard_interface(wrapper)

        win = wrapper.nodes.new("NodeGroupInput")
        wcore = wrapper.nodes.new("GeometryNodeGroup")
        wcore.name = "PNW_CoreNode"
        wcore.node_tree = core
        wout = wrapper.nodes.new("NodeGroupOutput")
        win.location = (-360, 0)
        wcore.location = (0, 0)
        wout.location = (360, 0)

        for name, _, _ in STANDARD_INPUT_SOCKETS:
            wrapper.links.new(win.outputs[name], wcore.inputs[name])
        wrapper.links.new(wcore.outputs["Geometry"], wout.inputs["Geometry"])
        wrapper[FINGERPRINT_KEY] = pnw_subgroups.tree_fingerprint(wrapper)
        return wrapper, False

    # Preserve existing wrapper! Ensure standard sockets exist, update PNW_CoreNode reference, keep user nodes/links
    manual_edited = pnw_subgroups.is_tree_manually_edited(existing)
    _ensure_standard_interface(existing)
    core_node = existing.nodes.get("PNW_CoreNode")
    if core_node is None:
        core_node = next((n for n in existing.nodes if n.bl_idname == "GeometryNodeGroup"), None)
    if core_node is not None:
        if core_node.node_tree != core:
            core_node.node_tree = core
        win = next((n for n in existing.nodes if n.bl_idname == "NodeGroupInput"), None)
        if win is not None:
            for name, _, _ in STANDARD_INPUT_SOCKETS:
                if name in core_node.inputs and not core_node.inputs[name].is_linked and name in win.outputs:
                    existing.links.new(win.outputs[name], core_node.inputs[name])
    return existing, manual_edited


def build_or_update_workflow(obj: bpy.types.Object, request: dict[str, Any]) -> dict[str, Any]:
    op = request["operation"]
    mod_name = request["modifier_name"]
    force_rebuild = bool(request.get("force_rebuild", False))
    obj_state_key = f"{STATE_PROP}__{mod_name}"

    mod = obj.modifiers.get(mod_name)
    if op == "parameters":
        if mod is None or mod.type != "NODES" or mod.node_group is None:
            raise ValueError(f"Modifier '{mod_name}' with GeometryNodeTree not found on '{obj.name}'")
        apply_request_parameters(obj, mod, request)
        state = json.loads(obj.get(obj_state_key, "{}"))
        prev_hash = state.get("parameters_hash")
        hash_changed = prev_hash is not None and prev_hash != request["parameters_hash"]
        cache_invalidated = False
        if hash_changed:
            pnw_bake.invalidate_cache(obj, mod, request.get("bake_directory") or mod.bake_directory or None)
            cache_invalidated = True
        state.update(
            {
                "domain": request["domain"],
                "preset_id": request["preset_id"],
                "bridge_mode": request["bridge_mode"],
                "parameters": request["parameters"],
                "parameters_hash": request["parameters_hash"],
            }
        )
        obj[obj_state_key] = json.dumps(state, ensure_ascii=False)
        wrapper = mod.node_group
        core_node = wrapper.nodes.get("PNW_CoreNode") or next(
            (n for n in wrapper.nodes if n.bl_idname == "GeometryNodeGroup"), None
        )
        core_tree = core_node.node_tree if core_node else None
        return {
            "mode": "parameters_only",
            "wrapper_name": wrapper.name,
            "core_name": core_tree.name if core_tree else None,
            "manual_edits_preserved": pnw_subgroups.is_tree_manually_edited(wrapper),
            "cache_invalidated_on_hash_change": cache_invalidated,
            "parameters_hash": request["parameters_hash"],
            "graph_fingerprint": pnw_subgroups.tree_fingerprint(wrapper),
        }

    # build / update / adopt
    subgroups = pnw_subgroups.ensure_all_subgroups(force_rebuild=force_rebuild)
    safe_obj = obj.name.replace(" ", "_")
    safe_mod = mod_name.replace(" ", "_")
    core_name = f"PNW_Core_{safe_obj}_{safe_mod}"
    wrapper_name = f"PNW_Wrapper_{safe_obj}_{safe_mod}"

    if mod is None:
        mod = obj.modifiers.new(mod_name, "NODES")

    # Preserve previous state hash to detect parameter/graph invalidation
    prev_state = {}
    if obj_state_key in obj:
        try:
            prev_state = json.loads(obj[obj_state_key])
        except (TypeError, json.JSONDecodeError):
            prev_state = {}

    core = _build_core_tree(core_name, obj, request, subgroups, force_rebuild=force_rebuild)
    wrapper, wrapper_manual_edited = _build_or_preserve_wrapper(wrapper_name, core, force_rebuild=force_rebuild)

    if mod.node_group != wrapper:
        mod.node_group = wrapper

    apply_request_parameters(obj, mod, request)

    # Apply explicit bridge if bridge_mode != GN_NATIVE
    col_obj = bpy.data.objects.get(request["collider_object"]) if request.get("collider_object") else None
    bridge_info = pnw_bridge.apply_bridge(obj, request, collider_obj=col_obj)

    graph_fp = pnw_subgroups.tree_fingerprint(core)
    cache_invalidated = False
    if prev_state and (
        prev_state.get("parameters_hash") != request["parameters_hash"]
        or prev_state.get("graph_fingerprint") != graph_fp
        or prev_state.get("preset_id") != request["preset_id"]
    ):
        pnw_bake.invalidate_cache(obj, mod, request.get("bake_directory") or mod.bake_directory or None)
        cache_invalidated = True

    state = {
        "schema_version": request["schema_version"],
        "domain": request["domain"],
        "preset_id": request["preset_id"],
        "bridge_mode": request["bridge_mode"],
        "parameters": request["parameters"],
        "parameters_hash": request["parameters_hash"],
        "graph_fingerprint": graph_fp,
        "core_name": core.name,
        "wrapper_name": wrapper.name,
    }
    obj[obj_state_key] = json.dumps(state, ensure_ascii=False)

    edited_subgroups = [
        name for name, sg in subgroups.items() if pnw_subgroups.is_tree_manually_edited(sg)
    ]

    return {
        "mode": op,
        "wrapper_name": wrapper.name,
        "core_name": core.name,
        "subgroup_names": list(subgroups.keys()),
        "edited_subgroups_preserved": edited_subgroups,
        "manual_edits_preserved": wrapper_manual_edited or len(edited_subgroups) > 0,
        "cache_invalidated_on_hash_change": cache_invalidated,
        "bridge": bridge_info,
        "parameters_hash": request["parameters_hash"],
        "graph_fingerprint": graph_fp,
    }
