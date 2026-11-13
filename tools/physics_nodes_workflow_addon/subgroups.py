"""Modular, transparent, user-editable Geometry Node subgroups for Physics Nodes Workbench."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import bpy


SUBGROUP_ROLE_KEY = "pnw_subgroup_role"
SUBGROUP_FINGERPRINT_KEY = "pnw_last_fingerprint"


def add_socket(tree: bpy.types.NodeTree, name: str, in_out: str, socket_type: str, default_value: Any = None):
    sock = tree.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    if default_value is not None and hasattr(sock, "default_value"):
        sock.default_value = default_value
    return sock


def tree_fingerprint(tree: bpy.types.NodeTree) -> str:
    """Stable structural hash used to detect whether a user manually edited a node group."""
    nodes = sorted(
        (
            n.name,
            n.bl_idname,
            round(n.location.x, 1),
            round(n.location.y, 1),
        )
        for n in tree.nodes
    )
    links = sorted(
        (
            l.from_node.name,
            l.from_socket.identifier,
            l.to_node.name,
            l.to_socket.identifier,
        )
        for l in tree.links
    )
    ifaces = [
        (item.name, item.in_out, item.socket_type)
        for item in tree.interface.items_tree
        if item.item_type == "SOCKET"
    ]
    payload = {"nodes": nodes, "links": links, "ifaces": ifaces}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def is_tree_manually_edited(tree: bpy.types.NodeTree | None) -> bool:
    if tree is None:
        return False
    saved = tree.get(SUBGROUP_FINGERPRINT_KEY)
    if not saved:
        return False
    return str(saved) != tree_fingerprint(tree)


def _prepare_subgroup(name: str, role: str, force_rebuild: bool = False) -> tuple[bpy.types.NodeTree, bool]:
    """Return existing subgroup if user edited it (unless force_rebuild), else (re)build cleanly."""
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if not force_rebuild:
            return existing, False
        existing.nodes.clear()
        existing.interface.clear()
        tree = existing
    else:
        tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree[SUBGROUP_ROLE_KEY] = role
    return tree, True


def _finalize_subgroup(tree: bpy.types.NodeTree) -> bpy.types.NodeTree:
    tree[SUBGROUP_FINGERPRINT_KEY] = tree_fingerprint(tree)
    return tree


def _vmath(tree: bpy.types.NodeTree, op: str, loc: tuple[float, float]):
    n = tree.nodes.new("ShaderNodeVectorMath")
    n.operation = op
    n.location = loc
    return n


def _math(tree: bpy.types.NodeTree, op: str, loc: tuple[float, float]):
    n = tree.nodes.new("ShaderNodeMath")
    n.operation = op
    n.location = loc
    return n


def ensure_force_gravity_drag(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Force_GravityDrag", "force", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.02)
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-720, 0)
    gout.location = (740, 0)

    g_dt = _vmath(tree, "SCALE", (-480, 120))
    tree.links.new(gin.outputs["Gravity"], g_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], g_dt.inputs["Scale"])

    v_add = _vmath(tree, "ADD", (-280, 140))
    tree.links.new(gin.outputs["Velocity"], v_add.inputs[0])
    tree.links.new(g_dt.outputs["Vector"], v_add.inputs[1])

    drag_dt = _math(tree, "MULTIPLY", (-480, -80))
    tree.links.new(gin.outputs["Drag"], drag_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], drag_dt.inputs[1])

    one_minus = _math(tree, "SUBTRACT", (-280, -80))
    one_minus.inputs[0].default_value = 1.0
    tree.links.new(drag_dt.outputs["Value"], one_minus.inputs[1])

    clamp_d = _math(tree, "MAXIMUM", (-100, -80))
    clamp_d.inputs[1].default_value = 0.0
    tree.links.new(one_minus.outputs["Value"], clamp_d.inputs[0])

    v_damp = _vmath(tree, "SCALE", (100, 120))
    tree.links.new(v_add.outputs["Vector"], v_damp.inputs[0])
    tree.links.new(clamp_d.outputs["Value"], v_damp.inputs["Scale"])

    # Note: Sleep velocity suppression belongs strictly in contact/collision steps,
    # never in gravity integration, otherwise high substeps trap stationary objects at t=0.
    tree.links.new(v_damp.outputs["Vector"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_force_wind_turbulence(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Force_WindTurbulence", "force", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Turbulence", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-760, 0)
    gout.location = (640, 0)

    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "4D"
    noise.inputs["Scale"].default_value = 1.6
    noise.inputs["Detail"].default_value = 2.0
    noise.location = (-520, -140)
    tree.links.new(gin.outputs["Position"], noise.inputs["Vector"])
    tree.links.new(gin.outputs["Time"], noise.inputs["W"])

    # Center noise color from [0, 1] -> [-0.5, 0.5]
    sub_half = _vmath(tree, "SUBTRACT", (-300, -140))
    sub_half.inputs[1].default_value = (0.5, 0.5, 0.5)
    tree.links.new(noise.outputs["Color"], sub_half.inputs[0])

    turb_scale = _vmath(tree, "SCALE", (-100, -140))
    tree.links.new(sub_half.outputs["Vector"], turb_scale.inputs[0])
    tree.links.new(gin.outputs["Turbulence"], turb_scale.inputs["Scale"])

    wind_total = _vmath(tree, "ADD", (100, -40))
    tree.links.new(gin.outputs["Wind Vector"], wind_total.inputs[0])
    tree.links.new(turb_scale.outputs["Vector"], wind_total.inputs[1])

    dv = _vmath(tree, "SCALE", (280, -40))
    tree.links.new(wind_total.outputs["Vector"], dv.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], dv.inputs["Scale"])

    v_out = _vmath(tree, "ADD", (460, 40))
    tree.links.new(gin.outputs["Velocity"], v_out.inputs[0])
    tree.links.new(dv.outputs["Vector"], v_out.inputs[1])
    tree.links.new(v_out.outputs["Vector"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_collide_ground_plane(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Collide_GroundPlane", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-860, 0)
    gout.location = (860, 0)

    floor_z = _math(tree, "ADD", (-620, 40))
    tree.links.new(gin.outputs["Ground Z"], floor_z.inputs[0])
    tree.links.new(gin.outputs["Radius"], floor_z.inputs[1])

    sep_p = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_p.location = (-620, 200)
    tree.links.new(gin.outputs["Position"], sep_p.inputs["Vector"])

    hit = _math(tree, "LESS_THAN", (-380, 120))
    tree.links.new(sep_p.outputs["Z"], hit.inputs[0])
    tree.links.new(floor_z.outputs["Value"], hit.inputs[1])

    comb_p = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_p.location = (-360, 300)
    tree.links.new(sep_p.outputs["X"], comb_p.inputs["X"])
    tree.links.new(sep_p.outputs["Y"], comb_p.inputs["Y"])
    tree.links.new(floor_z.outputs["Value"], comb_p.inputs["Z"])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (520, 220)
    tree.links.new(hit.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(comb_p.outputs["Vector"], sw_p.inputs["True"])

    sep_v = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_v.location = (-620, -200)
    tree.links.new(gin.outputs["Velocity"], sep_v.inputs["Vector"])

    # Tangential friction damp factor = max(0, 1 - Friction)
    fric_damp = _math(tree, "SUBTRACT", (-400, -120))
    fric_damp.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Friction"], fric_damp.inputs[1])

    vx = _math(tree, "MULTIPLY", (-180, -100))
    tree.links.new(sep_v.outputs["X"], vx.inputs[0])
    tree.links.new(fric_damp.outputs["Value"], vx.inputs[1])

    vy = _math(tree, "MULTIPLY", (-180, -220))
    tree.links.new(sep_v.outputs["Y"], vy.inputs[0])
    tree.links.new(fric_damp.outputs["Value"], vy.inputs[1])

    # Only bounce if downward velocity magnitude > Sleep Velocity; otherwise clamp vz=0 (resting stability!)
    vz_abs = _math(tree, "ABSOLUTE", (-400, -340))
    tree.links.new(sep_v.outputs["Z"], vz_abs.inputs[0])

    gt_sleep = _math(tree, "GREATER_THAN", (-220, -440))
    tree.links.new(vz_abs.outputs["Value"], gt_sleep.inputs[0])
    tree.links.new(gin.outputs["Sleep Velocity"], gt_sleep.inputs[1])

    vz_bounce = _math(tree, "MULTIPLY", (-180, -340))
    tree.links.new(vz_abs.outputs["Value"], vz_bounce.inputs[0])
    tree.links.new(gin.outputs["Restitution"], vz_bounce.inputs[1])

    vz_final = _math(tree, "MULTIPLY", (20, -360))
    tree.links.new(vz_bounce.outputs["Value"], vz_final.inputs[0])
    tree.links.new(gt_sleep.outputs["Value"], vz_final.inputs[1])

    # Only reflect if vz < 0 (moving into floor), else keep existing vz
    vz_neg = _math(tree, "LESS_THAN", (20, -480))
    vz_neg.inputs[1].default_value = 0.0
    tree.links.new(sep_v.outputs["Z"], vz_neg.inputs[0])

    sw_vz = tree.nodes.new("GeometryNodeSwitch")
    sw_vz.input_type = "FLOAT"
    sw_vz.location = (180, -400)
    tree.links.new(vz_neg.outputs["Value"], sw_vz.inputs["Switch"])
    tree.links.new(sep_v.outputs["Z"], sw_vz.inputs["False"])
    tree.links.new(vz_final.outputs["Value"], sw_vz.inputs["True"])

    comb_v = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_v.location = (340, -220)
    tree.links.new(vx.outputs["Value"], comb_v.inputs["X"])
    tree.links.new(vy.outputs["Value"], comb_v.inputs["Y"])
    tree.links.new(sw_vz.outputs["Output"], comb_v.inputs["Z"])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (560, -140)
    tree.links.new(hit.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(comb_v.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_collide_mesh_surface(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Collider response against an arbitrary Mesh Object using Proximity + SampleNearestSurface normal."""
    tree, need_build = _prepare_subgroup("PN_Collide_MeshSurface", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1040, 0)
    gout.location = (1040, 0)

    obj_info = tree.nodes.new("GeometryNodeObjectInfo")
    obj_info.transform_space = "RELATIVE"
    obj_info.location = (-840, 260)
    tree.links.new(gin.outputs["Collider"], obj_info.inputs["Object"])

    realize = tree.nodes.new("GeometryNodeRealizeInstances")
    realize.location = (-660, 260)
    tree.links.new(obj_info.outputs["Geometry"], realize.inputs["Geometry"])

    prox = tree.nodes.new("GeometryNodeProximity")
    prox.target_element = "FACES"
    prox.location = (-460, 320)
    tree.links.new(realize.outputs["Geometry"], prox.inputs["Geometry"])
    tree.links.new(gin.outputs["Position"], prox.inputs["Sample Position"])

    normal_in = tree.nodes.new("GeometryNodeInputNormal")
    normal_in.location = (-660, 80)

    samp_norm = tree.nodes.new("GeometryNodeSampleNearestSurface")
    samp_norm.data_type = "FLOAT_VECTOR"
    samp_norm.location = (-460, 80)
    tree.links.new(realize.outputs["Geometry"], samp_norm.inputs["Mesh"])
    tree.links.new(normal_in.outputs["Normal"], samp_norm.inputs["Value"])
    tree.links.new(gin.outputs["Position"], samp_norm.inputs["Sample Position"])

    n_unit = _vmath(tree, "NORMALIZE", (-260, 80))
    tree.links.new(samp_norm.outputs["Value"], n_unit.inputs[0])

    # Delta vector from surface closest point to particle: d = P - P_surf
    d_vec = _vmath(tree, "SUBTRACT", (-260, 260))
    tree.links.new(gin.outputs["Position"], d_vec.inputs[0])
    tree.links.new(prox.outputs["Position"], d_vec.inputs[1])

    signed_dist = _vmath(tree, "DOT_PRODUCT", (-80, 260))
    tree.links.new(d_vec.outputs["Vector"], signed_dist.inputs[0])
    tree.links.new(n_unit.outputs["Vector"], signed_dist.inputs[1])

    # Hit condition:
    # Must be geometrically close to surface (prox.Distance < Contact Thickness * 2.5)
    # AND inside/within contact shell (signed_dist < Contact Thickness), avoiding infinite plane extension outside mesh bounds.
    max_reach = _math(tree, "MULTIPLY", (-80, 380))
    max_reach.inputs[1].default_value = 2.5
    tree.links.new(gin.outputs["Contact Thickness"], max_reach.inputs[0])

    near_surf = _math(tree, "LESS_THAN", (100, 380))
    tree.links.new(prox.outputs["Distance"], near_surf.inputs[0])
    tree.links.new(max_reach.outputs["Value"], near_surf.inputs[1])

    lt_thick = _math(tree, "LESS_THAN", (100, 240))
    tree.links.new(signed_dist.outputs["Value"], lt_thick.inputs[0])
    tree.links.new(gin.outputs["Contact Thickness"], lt_thick.inputs[1])

    cond_depth = _math(tree, "MULTIPLY", (260, 280))
    tree.links.new(lt_thick.outputs["Value"], cond_depth.inputs[0])
    tree.links.new(near_surf.outputs["Value"], cond_depth.inputs[1])

    cond_valid = _math(tree, "MULTIPLY", (420, 280))
    tree.links.new(cond_depth.outputs["Value"], cond_valid.inputs[0])
    tree.links.new(prox.outputs["Is Valid"], cond_valid.inputs[1])

    # Push position out along normal: push_dist = Contact Thickness - signed_dist
    push_dist = _math(tree, "SUBTRACT", (100, 60))
    tree.links.new(gin.outputs["Contact Thickness"], push_dist.inputs[0])
    tree.links.new(signed_dist.outputs["Value"], push_dist.inputs[1])

    push_vec = _vmath(tree, "SCALE", (280, 60))
    tree.links.new(n_unit.outputs["Vector"], push_vec.inputs[0])
    tree.links.new(push_dist.outputs["Value"], push_vec.inputs["Scale"])

    p_pushed = _vmath(tree, "ADD", (460, 60))
    tree.links.new(gin.outputs["Position"], p_pushed.inputs[0])
    tree.links.new(push_vec.outputs["Vector"], p_pushed.inputs[1])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (760, 180)
    tree.links.new(cond_valid.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(p_pushed.outputs["Vector"], sw_p.inputs["True"])

    # Velocity reflection against surface normal when v_n = dot(V, N) < 0
    v_n = _vmath(tree, "DOT_PRODUCT", (-80, -140))
    tree.links.new(gin.outputs["Velocity"], v_n.inputs[0])
    tree.links.new(n_unit.outputs["Vector"], v_n.inputs[1])

    v_n_vec = _vmath(tree, "SCALE", (100, -140))
    tree.links.new(n_unit.outputs["Vector"], v_n_vec.inputs[0])
    tree.links.new(v_n.outputs["Value"], v_n_vec.inputs["Scale"])

    v_t = _vmath(tree, "SUBTRACT", (280, -100))
    tree.links.new(gin.outputs["Velocity"], v_t.inputs[0])
    tree.links.new(v_n_vec.outputs["Vector"], v_t.inputs[1])

    fric_factor = _math(tree, "SUBTRACT", (280, -240))
    fric_factor.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Friction"], fric_factor.inputs[1])

    v_t_damp = _vmath(tree, "SCALE", (460, -100))
    tree.links.new(v_t.outputs["Vector"], v_t_damp.inputs[0])
    tree.links.new(fric_factor.outputs["Value"], v_t_damp.inputs["Scale"])

    neg_rest = _math(tree, "MULTIPLY", (280, -360))
    neg_rest.inputs[0].default_value = -1.0
    tree.links.new(gin.outputs["Restitution"], neg_rest.inputs[1])

    v_n_bounce = _vmath(tree, "SCALE", (460, -300))
    tree.links.new(v_n_vec.outputs["Vector"], v_n_bounce.inputs[0])
    tree.links.new(neg_rest.outputs["Value"], v_n_bounce.inputs["Scale"])

    v_col = _vmath(tree, "ADD", (620, -200))
    tree.links.new(v_t_damp.outputs["Vector"], v_col.inputs[0])
    tree.links.new(v_n_bounce.outputs["Vector"], v_col.inputs[1])

    # Only alter velocity if colliding AND moving toward surface (v_n < 0)
    moving_in = _math(tree, "LESS_THAN", (460, -460))
    moving_in.inputs[1].default_value = 0.0
    tree.links.new(v_n.outputs["Value"], moving_in.inputs[0])

    vel_cond = _math(tree, "MULTIPLY", (640, -420))
    tree.links.new(cond_valid.outputs["Value"], vel_cond.inputs[0])
    tree.links.new(moving_in.outputs["Value"], vel_cond.inputs[1])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (800, -180)
    tree.links.new(vel_cond.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(v_col.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_constraint_pin_anchor(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Constraint_PinAnchor", "constraint", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Rest Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Pin Weight", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-520, 0)
    gout.location = (520, 0)

    is_pinned = _math(tree, "GREATER_THAN", (-240, 60))
    is_pinned.inputs[1].default_value = 0.5
    tree.links.new(gin.outputs["Pin Weight"], is_pinned.inputs[0])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (180, 100)
    tree.links.new(is_pinned.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(gin.outputs["Rest Position"], sw_p.inputs["True"])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (180, -100)
    sw_v.inputs["True"].default_value = (0.0, 0.0, 0.0)
    tree.links.new(is_pinned.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_constraint_distance_chain(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Elastic distance tether to an anchor point (activated when Stiffness > 0 and Rest Length > 0)."""
    tree, need_build = _prepare_subgroup("PN_Constraint_DistanceChain", "constraint", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Anchor", "INPUT", "NodeSocketVector", (0.0, 0.0, 2.5))
    add_socket(tree, "Rest Length", "INPUT", "NodeSocketFloat", 1.5)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-780, 0)
    gout.location = (780, 0)

    diff = _vmath(tree, "SUBTRACT", (-520, 120))
    tree.links.new(gin.outputs["Position"], diff.inputs[0])
    tree.links.new(gin.outputs["Anchor"], diff.inputs[1])

    dist = _vmath(tree, "LENGTH", (-320, 40))
    tree.links.new(diff.outputs["Vector"], dist.inputs[0])

    dir_v = _vmath(tree, "NORMALIZE", (-320, 200))
    tree.links.new(diff.outputs["Vector"], dir_v.inputs[0])

    # Excess stretch = max(0, dist - Rest Length)
    excess = _math(tree, "SUBTRACT", (-140, 40))
    tree.links.new(dist.outputs["Value"], excess.inputs[0])
    tree.links.new(gin.outputs["Rest Length"], excess.inputs[1])

    pos_excess = _math(tree, "MAXIMUM", (20, 40))
    pos_excess.inputs[1].default_value = 0.0
    tree.links.new(excess.outputs["Value"], pos_excess.inputs[0])

    corr_mag = _math(tree, "MULTIPLY", (180, 40))
    tree.links.new(pos_excess.outputs["Value"], corr_mag.inputs[0])
    tree.links.new(gin.outputs["Stiffness"], corr_mag.inputs[1])

    corr_vec = _vmath(tree, "SCALE", (340, 120))
    tree.links.new(dir_v.outputs["Vector"], corr_vec.inputs[0])
    tree.links.new(corr_mag.outputs["Value"], corr_vec.inputs["Scale"])

    p_new = _vmath(tree, "SUBTRACT", (520, 120))
    tree.links.new(gin.outputs["Position"], p_new.inputs[0])
    tree.links.new(corr_vec.outputs["Vector"], p_new.inputs[1])

    # Active only when Rest Length > 0 and Stiffness > 0
    has_len = _math(tree, "GREATER_THAN", (-140, -180))
    has_len.inputs[1].default_value = 1e-4
    tree.links.new(gin.outputs["Rest Length"], has_len.inputs[0])

    has_stiff = _math(tree, "GREATER_THAN", (-140, -300))
    has_stiff.inputs[1].default_value = 1e-4
    tree.links.new(gin.outputs["Stiffness"], has_stiff.inputs[0])

    active = _math(tree, "MULTIPLY", (80, -240))
    tree.links.new(has_len.outputs["Value"], active.inputs[0])
    tree.links.new(has_stiff.outputs["Value"], active.inputs[1])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (640, 60)
    tree.links.new(active.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(p_new.outputs["Vector"], sw_p.inputs["True"])

    # Dampen outward radial velocity softly via position correction difference
    v_corr = _vmath(tree, "SCALE", (340, -140))
    tree.links.new(corr_vec.outputs["Vector"], v_corr.inputs[0])
    v_corr.inputs["Scale"].default_value = 8.0

    v_new = _vmath(tree, "SUBTRACT", (520, -140))
    tree.links.new(gin.outputs["Velocity"], v_new.inputs[0])
    tree.links.new(v_corr.outputs["Vector"], v_new.inputs[1])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (640, -140)
    tree.links.new(active.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(v_new.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_solver_rigid_scatter_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Substep solver composing Gravity/Drag + Integration + DistanceChain + MeshCollide + GroundCollide + Rotation."""
    sg_force = ensure_force_gravity_drag(force_rebuild)
    sg_wind = ensure_force_wind_turbulence(force_rebuild)
    sg_chain = ensure_constraint_distance_chain(force_rebuild)
    sg_mesh = ensure_collide_mesh_surface(force_rebuild)
    sg_ground = ensure_collide_ground_plane(force_rebuild)

    tree, need_build = _prepare_subgroup("PN_Solver_RigidScatterStep", "solver", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Angular Spin", "INPUT", "NodeSocketFloat", 1.2)
    add_socket(tree, "Anchor", "INPUT", "NodeSocketVector", (0.0, 0.0, 2.5))
    add_socket(tree, "Rest Length", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1200, 0)
    gout.location = (1280, 0)

    pos_in = tree.nodes.new("GeometryNodeInputPosition")
    pos_in.location = (-1000, -180)
    vel_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_in.data_type = "FLOAT_VECTOR"
    vel_in.inputs["Name"].default_value = "pnw_velocity"
    vel_in.location = (-1000, -340)
    rot_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rot_in.data_type = "FLOAT_VECTOR"
    rot_in.inputs["Name"].default_value = "pnw_rotation"
    rot_in.location = (-1000, -500)

    n_force = tree.nodes.new("GeometryNodeGroup")
    n_force.node_tree = sg_force
    n_force.location = (-740, -180)
    tree.links.new(vel_in.outputs["Attribute"], n_force.inputs["Velocity"])
    tree.links.new(gin.outputs["Gravity"], n_force.inputs["Gravity"])
    tree.links.new(gin.outputs["Drag"], n_force.inputs["Drag"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_force.inputs["Sleep Velocity"])
    tree.links.new(gin.outputs["Delta Time"], n_force.inputs["Delta Time"])

    n_wind = tree.nodes.new("GeometryNodeGroup")
    n_wind.node_tree = sg_wind
    n_wind.location = (-500, -180)
    tree.links.new(pos_in.outputs["Position"], n_wind.inputs["Position"])
    tree.links.new(n_force.outputs["Velocity"], n_wind.inputs["Velocity"])
    tree.links.new(gin.outputs["Wind Vector"], n_wind.inputs["Wind Vector"])
    tree.links.new(gin.outputs["Wind Turbulence"], n_wind.inputs["Turbulence"])
    tree.links.new(gin.outputs["Time"], n_wind.inputs["Time"])
    tree.links.new(gin.outputs["Delta Time"], n_wind.inputs["Delta Time"])

    # Integrate position: p_int = pos + v * dt
    step_dx = _vmath(tree, "SCALE", (-280, -180))
    tree.links.new(n_wind.outputs["Velocity"], step_dx.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step_dx.inputs["Scale"])

    p_int = _vmath(tree, "ADD", (-100, -180))
    tree.links.new(pos_in.outputs["Position"], p_int.inputs[0])
    tree.links.new(step_dx.outputs["Vector"], p_int.inputs[1])

    # Optional distance tether constraint
    n_chain = tree.nodes.new("GeometryNodeGroup")
    n_chain.node_tree = sg_chain
    n_chain.location = (80, -180)
    tree.links.new(p_int.outputs["Vector"], n_chain.inputs["Position"])
    tree.links.new(n_wind.outputs["Velocity"], n_chain.inputs["Velocity"])
    tree.links.new(gin.outputs["Anchor"], n_chain.inputs["Anchor"])
    tree.links.new(gin.outputs["Rest Length"], n_chain.inputs["Rest Length"])
    tree.links.new(gin.outputs["Stiffness"], n_chain.inputs["Stiffness"])

    # Mesh surface collision (automatically skipped if Collider is None because Proximity.Is Valid = False)
    n_mesh = tree.nodes.new("GeometryNodeGroup")
    n_mesh.node_tree = sg_mesh
    n_mesh.location = (280, -180)
    tree.links.new(n_chain.outputs["Position"], n_mesh.inputs["Position"])
    tree.links.new(n_chain.outputs["Velocity"], n_mesh.inputs["Velocity"])
    tree.links.new(gin.outputs["Collider"], n_mesh.inputs["Collider"])
    tree.links.new(gin.outputs["Contact Thickness"], n_mesh.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], n_mesh.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_mesh.inputs["Friction"])

    # Ground plane collision
    n_ground = tree.nodes.new("GeometryNodeGroup")
    n_ground.node_tree = sg_ground
    n_ground.location = (480, -180)
    tree.links.new(n_mesh.outputs["Position"], n_ground.inputs["Position"])
    tree.links.new(n_mesh.outputs["Velocity"], n_ground.inputs["Velocity"])
    tree.links.new(gin.outputs["Ground Z"], n_ground.inputs["Ground Z"])
    tree.links.new(gin.outputs["Radius"], n_ground.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_ground.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_ground.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_ground.inputs["Sleep Velocity"])

    # Update Position
    set_p = tree.nodes.new("GeometryNodeSetPosition")
    set_p.location = (700, 40)
    tree.links.new(gin.outputs["Geometry"], set_p.inputs["Geometry"])
    tree.links.new(n_ground.outputs["Position"], set_p.inputs["Position"])

    # Store Velocity
    st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_vel.data_type = "FLOAT_VECTOR"
    st_vel.domain = "POINT"
    st_vel.inputs["Name"].default_value = "pnw_velocity"
    st_vel.location = (880, 40)
    tree.links.new(set_p.outputs["Geometry"], st_vel.inputs["Geometry"])
    tree.links.new(n_ground.outputs["Velocity"], st_vel.inputs["Value"])

    # Update angular rotation vector proportional to linear velocity (rolling spin) when above sleep speed
    sep_v = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_v.location = (480, -480)
    tree.links.new(n_ground.outputs["Velocity"], sep_v.inputs["Vector"])

    comb_omega = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_omega.location = (660, -480)
    # Roll around Y from vx and X from -vy, plus z spin
    neg_vy = _math(tree, "MULTIPLY", (560, -560))
    neg_vy.inputs[1].default_value = -1.0
    tree.links.new(sep_v.outputs["Y"], neg_vy.inputs[0])
    tree.links.new(neg_vy.outputs["Value"], comb_omega.inputs["X"])
    tree.links.new(sep_v.outputs["X"], comb_omega.inputs["Y"])
    tree.links.new(sep_v.outputs["Z"], comb_omega.inputs["Z"])

    spin_dt = _math(tree, "MULTIPLY", (660, -640))
    tree.links.new(gin.outputs["Angular Spin"], spin_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], spin_dt.inputs[1])

    d_rot = _vmath(tree, "SCALE", (820, -480))
    tree.links.new(comb_omega.outputs["Vector"], d_rot.inputs[0])
    tree.links.new(spin_dt.outputs["Value"], d_rot.inputs["Scale"])

    rot_next = _vmath(tree, "ADD", (960, -440))
    tree.links.new(rot_in.outputs["Attribute"], rot_next.inputs[0])
    tree.links.new(d_rot.outputs["Vector"], rot_next.inputs[1])

    st_rot = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_rot.data_type = "FLOAT_VECTOR"
    st_rot.domain = "POINT"
    st_rot.inputs["Name"].default_value = "pnw_rotation"
    st_rot.location = (1080, 40)
    tree.links.new(st_vel.outputs["Geometry"], st_rot.inputs["Geometry"])
    tree.links.new(rot_next.outputs["Vector"], st_rot.inputs["Value"])

    tree.links.new(st_rot.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_solver_cloth_rope_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Cloth & Rope PBD substep solver: Gravity + Wind + Topology Blur Relaxation + Colliders + Pin Anchor."""
    sg_force = ensure_force_gravity_drag(force_rebuild)
    sg_wind = ensure_force_wind_turbulence(force_rebuild)
    sg_mesh = ensure_collide_mesh_surface(force_rebuild)
    sg_ground = ensure_collide_ground_plane(force_rebuild)
    sg_pin = ensure_constraint_pin_anchor(force_rebuild)

    tree, need_build = _prepare_subgroup("PN_Solver_ClothRopeStep", "solver", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.008)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (4.0, 1.0, 0.2))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 1.8)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.72)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.1)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.4)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1200, 0)
    gout.location = (1320, 0)

    pos_in = tree.nodes.new("GeometryNodeInputPosition")
    pos_in.location = (-1000, -180)
    vel_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_in.data_type = "FLOAT_VECTOR"
    vel_in.inputs["Name"].default_value = "pnw_velocity"
    vel_in.location = (-1000, -340)
    rest_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_in.data_type = "FLOAT_VECTOR"
    rest_in.inputs["Name"].default_value = "pnw_rest_pos"
    rest_in.location = (-1000, -500)
    pin_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    pin_in.data_type = "FLOAT"
    pin_in.inputs["Name"].default_value = "pnw_pinned"
    pin_in.location = (-1000, -640)

    n_force = tree.nodes.new("GeometryNodeGroup")
    n_force.node_tree = sg_force
    n_force.location = (-740, -180)
    tree.links.new(vel_in.outputs["Attribute"], n_force.inputs["Velocity"])
    tree.links.new(gin.outputs["Gravity"], n_force.inputs["Gravity"])
    tree.links.new(gin.outputs["Drag"], n_force.inputs["Drag"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_force.inputs["Sleep Velocity"])
    tree.links.new(gin.outputs["Delta Time"], n_force.inputs["Delta Time"])

    n_wind = tree.nodes.new("GeometryNodeGroup")
    n_wind.node_tree = sg_wind
    n_wind.location = (-520, -180)
    tree.links.new(pos_in.outputs["Position"], n_wind.inputs["Position"])
    tree.links.new(n_force.outputs["Velocity"], n_wind.inputs["Velocity"])
    tree.links.new(gin.outputs["Wind Vector"], n_wind.inputs["Wind Vector"])
    tree.links.new(gin.outputs["Wind Turbulence"], n_wind.inputs["Turbulence"])
    tree.links.new(gin.outputs["Time"], n_wind.inputs["Time"])
    tree.links.new(gin.outputs["Delta Time"], n_wind.inputs["Delta Time"])

    step_dx = _vmath(tree, "SCALE", (-320, -180))
    tree.links.new(n_wind.outputs["Velocity"], step_dx.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step_dx.inputs["Scale"])

    p_pred = _vmath(tree, "ADD", (-140, -180))
    tree.links.new(pos_in.outputs["Position"], p_pred.inputs[0])
    tree.links.new(step_dx.outputs["Vector"], p_pred.inputs[1])

    # Set predicted position temporarily so BlurAttribute operates on mesh topology at predicted pose
    set_pred = tree.nodes.new("GeometryNodeSetPosition")
    set_pred.location = (40, 40)
    tree.links.new(gin.outputs["Geometry"], set_pred.inputs["Geometry"])
    tree.links.new(p_pred.outputs["Vector"], set_pred.inputs["Position"])

    # Displacement from rest pose: blurring (p_pred - rest_in) preserves rest geometry & avoids shrinkage
    u_pred = _vmath(tree, "SUBTRACT", (40, -180))
    tree.links.new(p_pred.outputs["Vector"], u_pred.inputs[0])
    tree.links.new(rest_in.outputs["Attribute"], u_pred.inputs[1])

    blur = tree.nodes.new("GeometryNodeBlurAttribute")
    blur.data_type = "FLOAT_VECTOR"
    blur.inputs["Iterations"].default_value = 3
    blur.location = (200, -180)
    tree.links.new(u_pred.outputs["Vector"], blur.inputs["Value"])
    tree.links.new(gin.outputs["Stiffness"], blur.inputs["Weight"])

    p_relaxed = _vmath(tree, "ADD", (300, -180))
    tree.links.new(rest_in.outputs["Attribute"], p_relaxed.inputs[0])
    tree.links.new(blur.outputs["Value"], p_relaxed.inputs[1])

    # Blend blurred displacement correction mildly (25% relaxed + 75% predicted)
    mix_p = tree.nodes.new("ShaderNodeMix")
    mix_p.data_type = "VECTOR"
    mix_p.inputs["Factor"].default_value = 0.25
    mix_p.location = (420, -180)
    tree.links.new(p_pred.outputs["Vector"], mix_p.inputs["A"])
    tree.links.new(p_relaxed.outputs["Vector"], mix_p.inputs["B"])

    # Couple velocity to PBD relaxed displacement (mix_p - pos_in) / dt to prevent free-fall velocity windup
    safe_dt = _math(tree, "MAXIMUM", (400, -360))
    safe_dt.inputs[1].default_value = 1e-4
    tree.links.new(gin.outputs["Delta Time"], safe_dt.inputs[0])

    rel_dx = _vmath(tree, "SUBTRACT", (400, -480))
    tree.links.new(mix_p.outputs["Result"], rel_dx.inputs[0])
    tree.links.new(pos_in.outputs["Position"], rel_dx.inputs[1])

    inv_dt = _math(tree, "DIVIDE", (500, -360))
    inv_dt.inputs[0].default_value = 1.0
    tree.links.new(safe_dt.outputs["Value"], inv_dt.inputs[1])

    v_relaxed = _vmath(tree, "SCALE", (500, -480))
    tree.links.new(rel_dx.outputs["Vector"], v_relaxed.inputs[0])
    tree.links.new(inv_dt.outputs["Value"], v_relaxed.inputs["Scale"])

    mix_v = tree.nodes.new("ShaderNodeMix")
    mix_v.data_type = "VECTOR"
    mix_v.inputs["Factor"].default_value = 0.45
    mix_v.location = (540, -320)
    tree.links.new(n_wind.outputs["Velocity"], mix_v.inputs["A"])
    tree.links.new(v_relaxed.outputs["Vector"], mix_v.inputs["B"])

    n_mesh = tree.nodes.new("GeometryNodeGroup")
    n_mesh.node_tree = sg_mesh
    n_mesh.location = (660, -180)
    tree.links.new(mix_p.outputs["Result"], n_mesh.inputs["Position"])
    tree.links.new(mix_v.outputs["Result"], n_mesh.inputs["Velocity"])
    tree.links.new(gin.outputs["Collider"], n_mesh.inputs["Collider"])
    tree.links.new(gin.outputs["Contact Thickness"], n_mesh.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], n_mesh.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_mesh.inputs["Friction"])

    n_ground = tree.nodes.new("GeometryNodeGroup")
    n_ground.node_tree = sg_ground
    n_ground.location = (760, -180)
    tree.links.new(n_mesh.outputs["Position"], n_ground.inputs["Position"])
    tree.links.new(n_mesh.outputs["Velocity"], n_ground.inputs["Velocity"])
    tree.links.new(gin.outputs["Ground Z"], n_ground.inputs["Ground Z"])
    tree.links.new(gin.outputs["Contact Thickness"], n_ground.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_ground.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_ground.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_ground.inputs["Sleep Velocity"])

    # Enforce pinned anchors
    n_pin = tree.nodes.new("GeometryNodeGroup")
    n_pin.node_tree = sg_pin
    n_pin.location = (940, -180)
    tree.links.new(n_ground.outputs["Position"], n_pin.inputs["Position"])
    tree.links.new(n_ground.outputs["Velocity"], n_pin.inputs["Velocity"])
    tree.links.new(rest_in.outputs["Attribute"], n_pin.inputs["Rest Position"])
    tree.links.new(pin_in.outputs["Attribute"], n_pin.inputs["Pin Weight"])

    set_final = tree.nodes.new("GeometryNodeSetPosition")
    set_final.location = (1080, 40)
    tree.links.new(set_pred.outputs["Geometry"], set_final.inputs["Geometry"])
    tree.links.new(n_pin.outputs["Position"], set_final.inputs["Position"])

    st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_vel.data_type = "FLOAT_VECTOR"
    st_vel.domain = "POINT"
    st_vel.inputs["Name"].default_value = "pnw_velocity"
    st_vel.location = (1200, 40)
    tree.links.new(set_final.outputs["Geometry"], st_vel.inputs["Geometry"])
    tree.links.new(n_pin.outputs["Velocity"], st_vel.inputs["Value"])

    tree.links.new(st_vel.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_solver_fluid_particle_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Particle advection + mesh obstacle deflection + ground splash + age advancement per substep."""
    sg_force = ensure_force_gravity_drag(force_rebuild)
    sg_wind = ensure_force_wind_turbulence(force_rebuild)
    sg_mesh = ensure_collide_mesh_surface(force_rebuild)
    sg_ground = ensure_collide_ground_plane(force_rebuild)

    tree, need_build = _prepare_subgroup("PN_Solver_FluidParticleStep", "solver", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.3, 0.0, 0.0))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 0.8)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.25)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.15)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1160, 0)
    gout.location = (1180, 0)

    pos_in = tree.nodes.new("GeometryNodeInputPosition")
    pos_in.location = (-960, -180)
    vel_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_in.data_type = "FLOAT_VECTOR"
    vel_in.inputs["Name"].default_value = "pnw_velocity"
    vel_in.location = (-960, -340)
    age_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    age_in.data_type = "FLOAT"
    age_in.inputs["Name"].default_value = "pnw_age"
    age_in.location = (-960, -500)

    n_force = tree.nodes.new("GeometryNodeGroup")
    n_force.node_tree = sg_force
    n_force.location = (-700, -180)
    tree.links.new(vel_in.outputs["Attribute"], n_force.inputs["Velocity"])
    tree.links.new(gin.outputs["Gravity"], n_force.inputs["Gravity"])
    tree.links.new(gin.outputs["Drag"], n_force.inputs["Drag"])
    n_force.inputs["Sleep Velocity"].default_value = 0.0
    tree.links.new(gin.outputs["Delta Time"], n_force.inputs["Delta Time"])

    n_wind = tree.nodes.new("GeometryNodeGroup")
    n_wind.node_tree = sg_wind
    n_wind.location = (-480, -180)
    tree.links.new(pos_in.outputs["Position"], n_wind.inputs["Position"])
    tree.links.new(n_force.outputs["Velocity"], n_wind.inputs["Velocity"])
    tree.links.new(gin.outputs["Wind Vector"], n_wind.inputs["Wind Vector"])
    tree.links.new(gin.outputs["Wind Turbulence"], n_wind.inputs["Turbulence"])
    tree.links.new(gin.outputs["Time"], n_wind.inputs["Time"])
    tree.links.new(gin.outputs["Delta Time"], n_wind.inputs["Delta Time"])

    step_dx = _vmath(tree, "SCALE", (-260, -180))
    tree.links.new(n_wind.outputs["Velocity"], step_dx.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step_dx.inputs["Scale"])

    p_next = _vmath(tree, "ADD", (-80, -180))
    tree.links.new(pos_in.outputs["Position"], p_next.inputs[0])
    tree.links.new(step_dx.outputs["Vector"], p_next.inputs[1])

    n_mesh = tree.nodes.new("GeometryNodeGroup")
    n_mesh.node_tree = sg_mesh
    n_mesh.location = (120, -180)
    tree.links.new(p_next.outputs["Vector"], n_mesh.inputs["Position"])
    tree.links.new(n_wind.outputs["Velocity"], n_mesh.inputs["Velocity"])
    tree.links.new(gin.outputs["Collider"], n_mesh.inputs["Collider"])
    tree.links.new(gin.outputs["Contact Thickness"], n_mesh.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], n_mesh.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_mesh.inputs["Friction"])

    n_ground = tree.nodes.new("GeometryNodeGroup")
    n_ground.node_tree = sg_ground
    n_ground.location = (340, -180)
    tree.links.new(n_mesh.outputs["Position"], n_ground.inputs["Position"])
    tree.links.new(n_mesh.outputs["Velocity"], n_ground.inputs["Velocity"])
    tree.links.new(gin.outputs["Ground Z"], n_ground.inputs["Ground Z"])
    tree.links.new(gin.outputs["Contact Thickness"], n_ground.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_ground.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_ground.inputs["Friction"])
    n_ground.inputs["Sleep Velocity"].default_value = 0.0

    set_p = tree.nodes.new("GeometryNodeSetPosition")
    set_p.location = (560, 40)
    tree.links.new(gin.outputs["Geometry"], set_p.inputs["Geometry"])
    tree.links.new(n_ground.outputs["Position"], set_p.inputs["Position"])

    st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_vel.data_type = "FLOAT_VECTOR"
    st_vel.domain = "POINT"
    st_vel.inputs["Name"].default_value = "pnw_velocity"
    st_vel.location = (760, 40)
    tree.links.new(set_p.outputs["Geometry"], st_vel.inputs["Geometry"])
    tree.links.new(n_ground.outputs["Velocity"], st_vel.inputs["Value"])

    age_add = _math(tree, "ADD", (560, -440))
    tree.links.new(age_in.outputs["Attribute"], age_add.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], age_add.inputs[1])

    st_age = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_age.data_type = "FLOAT"
    st_age.domain = "POINT"
    st_age.inputs["Name"].default_value = "pnw_age"
    st_age.location = (960, 40)
    tree.links.new(st_vel.outputs["Geometry"], st_age.inputs["Geometry"])
    tree.links.new(age_add.outputs["Value"], st_age.inputs["Value"])

    tree.links.new(st_age.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_post_instance_rigid(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_InstanceRigid", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Instance Collection", "INPUT", "NodeSocketCollection")
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-720, 0)
    gout.location = (720, 0)

    col_info = tree.nodes.new("GeometryNodeCollectionInfo")
    col_info.inputs["Separate Children"].default_value = True
    col_info.inputs["Reset Children"].default_value = True
    col_info.location = (-440, -180)
    tree.links.new(gin.outputs["Instance Collection"], col_info.inputs["Collection"])

    # Fallback procedural bevelled cube if collection has 0 instances
    fallback_cube = tree.nodes.new("GeometryNodeMeshCube")
    fallback_cube.inputs["Size"].default_value = (1.0, 1.0, 1.0)
    fallback_cube.location = (-440, -340)

    has_col = tree.nodes.new("GeometryNodeGeometryToInstance")
    has_col.location = (-440, -480)
    tree.links.new(fallback_cube.outputs["Mesh"], has_col.inputs["Geometry"])

    # Note: In GN, we check collection size via DomainSize(INSTANCES) > 0 to pick CollectionInfo vs fallback
    dom = tree.nodes.new("GeometryNodeAttributeDomainSize")
    dom.component = "INSTANCES"
    dom.location = (-240, -320)
    tree.links.new(col_info.outputs["Instances"], dom.inputs["Geometry"])

    gt0 = _math(tree, "GREATER_THAN", (-40, -320))
    gt0.inputs[1].default_value = 0.0
    tree.links.new(dom.outputs["Instance Count"], gt0.inputs[0])

    sw_inst = tree.nodes.new("GeometryNodeSwitch")
    sw_inst.input_type = "GEOMETRY"
    sw_inst.location = (140, -240)
    tree.links.new(gt0.outputs["Value"], sw_inst.inputs["Switch"])
    tree.links.new(has_col.outputs["Instances"], sw_inst.inputs["False"])
    tree.links.new(col_info.outputs["Instances"], sw_inst.inputs["True"])

    rot_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rot_attr.data_type = "FLOAT_VECTOR"
    rot_attr.inputs["Name"].default_value = "pnw_rotation"
    rot_attr.location = (-240, 180)

    idx_in = tree.nodes.new("GeometryNodeInputIndex")
    idx_in.location = (140, 200)

    inst_pts = tree.nodes.new("GeometryNodeInstanceOnPoints")
    inst_pts.inputs["Pick Instance"].default_value = True
    inst_pts.location = (380, 40)
    tree.links.new(gin.outputs["Points"], inst_pts.inputs["Points"])
    tree.links.new(sw_inst.outputs["Output"], inst_pts.inputs["Instance"])
    tree.links.new(idx_in.outputs["Index"], inst_pts.inputs["Instance Index"])
    tree.links.new(rot_attr.outputs["Attribute"], inst_pts.inputs["Rotation"])

    # Scale instances uniformly to Diameter (~2*Radius) when using fallback or normalized assets
    scale_mul = _math(tree, "MULTIPLY", (140, 80))
    scale_mul.inputs[1].default_value = 2.0
    tree.links.new(gin.outputs["Radius"], scale_mul.inputs[0])

    comb_s = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_s.location = (240, 80)
    tree.links.new(scale_mul.outputs["Value"], comb_s.inputs["X"])
    tree.links.new(scale_mul.outputs["Value"], comb_s.inputs["Y"])
    tree.links.new(scale_mul.outputs["Value"], comb_s.inputs["Z"])
    tree.links.new(comb_s.outputs["Vector"], inst_pts.inputs["Scale"])

    tree.links.new(inst_pts.outputs["Instances"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_post_fluid_sdf_mesh(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_FluidSDFMesh", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Particle Radius", "INPUT", "NodeSocketFloat", 0.16)
    add_socket(tree, "Voxel Size", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Mesh", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-460, 0)
    gout.location = (460, 0)

    p2sdf = tree.nodes.new("GeometryNodePointsToSDFGrid")
    p2sdf.location = (-140, 0)
    tree.links.new(gin.outputs["Points"], p2sdf.inputs["Points"])
    tree.links.new(gin.outputs["Particle Radius"], p2sdf.inputs["Radius"])
    tree.links.new(gin.outputs["Voxel Size"], p2sdf.inputs["Voxel Size"])

    g2m = tree.nodes.new("GeometryNodeGridToMesh")
    g2m.location = (120, 0)
    tree.links.new(p2sdf.outputs["SDF Grid"], g2m.inputs["Grid"])

    smooth = tree.nodes.new("GeometryNodeSetShadeSmooth")
    smooth.location = (280, 0)
    tree.links.new(g2m.outputs["Mesh"], smooth.inputs[0])
    tree.links.new(smooth.outputs[0], gout.inputs["Mesh"])
    return _finalize_subgroup(tree)


def ensure_post_smoke_volume(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_SmokeVolume", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Particle Radius", "INPUT", "NodeSocketFloat", 0.24)
    add_socket(tree, "Voxel Size", "INPUT", "NodeSocketFloat", 0.1)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-480, 0)
    gout.location = (520, 0)

    p2v = tree.nodes.new("GeometryNodePointsToVolume")
    p2v.inputs["Resolution Mode"].default_value = "Size"
    p2v.location = (-160, 100)
    tree.links.new(gin.outputs["Points"], p2v.inputs["Points"])
    tree.links.new(gin.outputs["Particle Radius"], p2v.inputs["Radius"])
    tree.links.new(gin.outputs["Voxel Size"], p2v.inputs["Voxel Size"])

    # Also build preview mesh alongside volume so Workbench/wireframe & mesh exporters see smoke geometry
    v2m = tree.nodes.new("GeometryNodeVolumeToMesh")
    v2m.inputs["Resolution Mode"].default_value = "Grid"
    v2m.inputs["Threshold"].default_value = 0.08
    v2m.location = (80, -80)
    tree.links.new(p2v.outputs["Volume"], v2m.inputs["Volume"])

    j = tree.nodes.new("GeometryNodeJoinGeometry")
    j.location = (300, 20)
    tree.links.new(p2v.outputs["Volume"], j.inputs["Geometry"])
    tree.links.new(v2m.outputs["Mesh"], j.inputs["Geometry"])
    tree.links.new(j.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_all_subgroups(force_rebuild: bool = False) -> dict[str, bpy.types.NodeTree]:
    return {
        "PN_Force_GravityDrag": ensure_force_gravity_drag(force_rebuild),
        "PN_Force_WindTurbulence": ensure_force_wind_turbulence(force_rebuild),
        "PN_Collide_GroundPlane": ensure_collide_ground_plane(force_rebuild),
        "PN_Collide_MeshSurface": ensure_collide_mesh_surface(force_rebuild),
        "PN_Constraint_PinAnchor": ensure_constraint_pin_anchor(force_rebuild),
        "PN_Constraint_DistanceChain": ensure_constraint_distance_chain(force_rebuild),
        "PN_Solver_RigidScatterStep": ensure_solver_rigid_scatter_step(force_rebuild),
        "PN_Solver_ClothRopeStep": ensure_solver_cloth_rope_step(force_rebuild),
        "PN_Solver_FluidParticleStep": ensure_solver_fluid_particle_step(force_rebuild),
        "PN_Post_InstanceRigid": ensure_post_instance_rigid(force_rebuild),
        "PN_Post_FluidSDFMesh": ensure_post_fluid_sdf_mesh(force_rebuild),
        "PN_Post_SmokeVolume": ensure_post_smoke_volume(force_rebuild),
    }
