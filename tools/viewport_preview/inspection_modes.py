"""Temporary Blender settings for viewport material and UV inspection."""

import math
import uuid

import bpy
import numpy as np


PASSES = (("Base Color", "AOV"), ("Roughness", "AOV"), ("Normal", "NORMAL"), ("AO", "AO"))


def _terminal_principled(tree):
    if tree is None:
        return None
    output = next((node for node in tree.nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    if output and output.inputs["Surface"].is_linked:
        node = output.inputs["Surface"].links[0].from_node
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def prepare_passes(view_layer, objects, restore):
    suffix = uuid.uuid4().hex[:8]
    names = {name: f"Preview_{name.replace(' ', '')}_{suffix}" for name, mode in PASSES if mode == "AOV"}
    for channel, aov_name in names.items():
        aov = view_layer.aovs.add()
        aov.name = aov_name
        aov.type = "VALUE" if channel == "Roughness" else "COLOR"
        restore.append(lambda aov=aov: view_layer.aovs.remove(aov))

    materials = {slot.material for obj in objects for slot in obj.material_slots if slot.material}
    unmeasured = []
    for material in sorted(materials, key=lambda mat: mat.name):
        tree = material.node_tree
        principled = _terminal_principled(tree)
        if not principled or not material.is_editable or not tree.is_editable:
            unmeasured.append(material.name)
            continue
        for channel, aov_name in names.items():
            node = tree.nodes.new("ShaderNodeOutputAOV")
            node.aov_name = aov_name
            restore.append(lambda tree=tree, node=node: tree.nodes.remove(node))
            src = principled.inputs[channel]
            dst = node.inputs["Value" if channel == "Roughness" else "Color"]
            if src.is_linked:
                tree.links.new(src.links[0].from_socket, dst)
            else:
                dst.default_value = src.default_value
    return names, unmeasured


def _stretch(mesh):
    layer = mesh.uv_layers.active
    result = np.full(len(mesh.polygons), math.inf, dtype=np.float64)
    if layer is None:
        return result
    mesh.calc_loop_triangles()
    ratios = np.zeros_like(result)
    for tri in mesh.loop_triangles:
        p0, p1, p2 = [mesh.vertices[index].co for index in tri.vertices]
        a, b = p1 - p0, p2 - p0
        length = a.length
        if length < 1e-10:
            continue
        ux = a / length
        perpendicular = b - ux * b.dot(ux)
        if perpendicular.length < 1e-10:
            continue
        uv0, uv1, uv2 = [layer.uv[index].vector for index in tri.loops]
        source = np.array([[length, b.dot(ux)], [0.0, perpendicular.length]])
        destination = np.array([tuple(uv1 - uv0), tuple(uv2 - uv0)]).T
        if not np.isfinite(destination).all():
            continue
        hi, lo = np.linalg.svd(np.linalg.solve(source.T, destination.T).T, compute_uv=False)
        ratio = hi / lo if lo >= 1e-12 else math.inf
        ratios[tri.polygon_index] = max(ratios[tri.polygon_index], ratio)
    populated = ratios > 0
    result[populated] = ratios[populated]
    return result


def prepare_uv(scene, view_layer, objects, mode, restore):
    override = bpy.data.materials.new("Preview_UV_Inspection")
    override.use_nodes = True
    tree = override.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    previous = view_layer.material_override
    view_layer.material_override = override
    def restore_override():
        view_layer.material_override = previous
        bpy.data.materials.remove(override)
    restore.append(restore_override)

    if mode == "uv_checker":
        coord = tree.nodes.new("ShaderNodeTexCoord")
        checker = tree.nodes.new("ShaderNodeTexChecker")
        checker.inputs["Scale"].default_value = 12
        checker.inputs["Color1"].default_value = (0.04, 0.17, 0.34, 1.0)
        checker.inputs["Color2"].default_value = (0.95, 0.95, 0.95, 1.0)
        tree.links.new(coord.outputs["UV"], checker.inputs["Vector"])
        tree.links.new(checker.outputs["Color"], emission.inputs["Color"])
        return {"uv_method": "UV-coordinate 12-by-12 checker"}

    name = f"PreviewStretch_{uuid.uuid4().hex[:8]}"
    attr = tree.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = name
    tree.links.new(attr.outputs["Color"], emission.inputs["Color"])
    measurements = []
    for obj in objects:
        if obj.type != "MESH" or not obj.is_editable or not obj.data.is_editable:
            continue
        source = obj.data
        scratch = source.copy()
        obj.data = scratch
        def restore_mesh(obj=obj, source=source, scratch=scratch):
            obj.data = source
            bpy.data.meshes.remove(scratch)
        restore.append(restore_mesh)
        ratios = _stretch(scratch)
        colors = scratch.color_attributes.new(name, "FLOAT_COLOR", "CORNER")
        uv_color = np.empty((len(scratch.loops), 4), dtype=np.float32)
        for polygon in scratch.polygons:
            ratio = ratios[polygon.index]
            width = min(1.0, math.log2(ratio) / 2)
            rgb = (width * 0.9 + 0.05, (1 - width) * 0.55 + 0.03, 0.09, 1.0)
            uv_color[polygon.loop_start:polygon.loop_start + polygon.loop_total] = rgb
        colors.data.foreach_set("color", uv_color.ravel())
        valid = ratios[np.isfinite(ratios)]
        measurements.append({"object": obj.name, "faces": len(ratios), "degenerate_faces": len(ratios) - len(valid),
                             "maximum_anisotropy": float(valid.max()) if len(valid) else None})
    if not measurements:
        raise ValueError("UV stretch requires at least one editable mesh target")
    return {"uv_method": "per-face maximum UV Jacobian anisotropy, green 1:1 to red 4:1 or higher",
            "measurements": measurements}
