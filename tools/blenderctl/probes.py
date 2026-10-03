# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, generated fixtures. These experiments are NOT production write commands."""
import math
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector


def check(value, message):
    if not value:
        raise AssertionError(message)


def empty():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def cube(name="测试方块"):
    mesh = bpy.data.meshes.new(name + "_Mesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def save_reopen(job):
    path = job / "中文 空格 fixture.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    return str(path)


def scene(job):
    empty()
    obj = cube()
    col = bpy.data.collections.new("家具 测试")
    bpy.context.scene.collection.children.link(col)
    col.objects.link(obj)
    bpy.context.scene.collection.objects.unlink(obj)
    obj.asset_mark()
    obj.asset_data.description = "CLI 中文验证"
    obj.asset_data.tags.new("subject:test")
    obj["asset_id"] = "37d7ed65-943b-4992-a9b6-5e4ed21d93cb"
    obj.location = (1, 2, 3)
    copy = bpy.data.objects.new("共享网格实例", obj.data)
    col.objects.link(copy)
    copy.parent = obj
    path = save_reopen(job)
    obj = bpy.data.objects["测试方块"]
    check(tuple(obj.location) == (1, 2, 3), "Transform did not persist")
    check(obj.data == bpy.data.objects["共享网格实例"].data, "Shared mesh lost")
    check(obj.asset_data.description == "CLI 中文验证", "Chinese metadata lost")
    check(obj.name in bpy.data.collections["家具 测试"].objects, "Collection link lost")
    return {"checks": ["BMesh cube", "collection links", "parent", "shared mesh", "Chinese asset metadata", "save and reopen"], "blend": path}


def geometry(job):
    empty()
    obj = cube()
    bevel = obj.modifiers.new("Bevel", "BEVEL")
    bevel.width = 0.15
    bevel.segments = 2
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    before = len(evaluated.data.vertices)
    check(before > 8, "Bevel did not evaluate")
    bpy.ops.object.modifier_apply(modifier=bevel.name)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    check("FINISHED" in bpy.ops.uv.smart_project(island_margin=0.03), "UV unwrap failed")
    bpy.ops.object.mode_set(mode="OBJECT")
    path = save_reopen(job)
    obj = bpy.data.objects["测试方块"]
    uv = obj.data.uv_layers.active
    check(len(obj.data.vertices) == before and uv is not None, "Geometry or UV missing")
    values = [tuple(loop.uv) for loop in uv.data]
    check(values and all(math.isfinite(v) and -1e-5 <= v <= 1.00001 for pair in values for v in pair), "Invalid UV extent")
    check(len(set(values)) > 4, "Degenerate UV")
    return {"checks": ["bevel evaluated", "modifier applied", "explicit edit context", "UV unwrap bounds", "reopened mesh"], "vertices": before, "uv_loops": len(values), "blend": path}


def nodes(job):
    empty()
    obj = cube()
    mat = bpy.data.materials.new("铜 材质")
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (0.7, 0.25, 0.08, 1)
    shader.inputs["Metallic"].default_value = 0.8
    obj.data.materials.append(mat)
    group = bpy.data.node_groups.new("程序化缩放", "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    inp = group.nodes.new("NodeGroupInput")
    out = group.nodes.new("NodeGroupOutput")
    transform = group.nodes.new("GeometryNodeTransform")
    transform.inputs["Scale"].default_value = (2, 1, 1)
    group.links.new(inp.outputs["Geometry"], transform.inputs["Geometry"])
    group.links.new(transform.outputs["Geometry"], out.inputs["Geometry"])
    mod = obj.modifiers.new("GeometryNodes", "NODES")
    mod.node_group = group
    path = save_reopen(job)
    obj = bpy.data.objects["测试方块"]
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    xs = [v.co.x for v in evaluated.data.vertices]
    check(abs(max(xs) - min(xs) - 4) < 1e-5, "Geometry node evaluation failed")
    check(abs(obj.data.materials[0].node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value - .8) < 1e-5, "Material parameter lost")
    return {"checks": ["shader assignment", "node group interface", "geometry node links", "evaluated x extent=4", "reopen"], "blend": path}


def animation(job):
    empty()
    obj = cube()
    arm = bpy.data.armatures.new("骨架数据")
    rig = bpy.data.objects.new("测试骨架", arm)
    bpy.context.scene.collection.objects.link(rig)
    obj.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bone = arm.edit_bones.new("Root")
    bone.head = (0, 0, 0)
    bone.tail = (0, 0, 2)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.vertex_groups.new(name="Root").add(list(range(8)), 1.0, "REPLACE")
    obj.modifiers.new("Armature", "ARMATURE").object = rig
    pose = rig.pose.bones["Root"]
    pose.location = (0, 0, 0)
    pose.keyframe_insert("location", frame=1)
    pose.location = (2, 0, 0)
    pose.keyframe_insert("location", frame=11)
    path = save_reopen(job)
    obj = bpy.data.objects["测试方块"]
    rig = bpy.data.objects["测试骨架"]
    samples = []
    for frame in (1, 6, 11):
        bpy.context.scene.frame_set(frame)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        samples.append(sum(v.co.x for v in evaluated.data.vertices) / len(evaluated.data.vertices))
    check(abs(samples[0]) < 1e-5 and abs(samples[-1] - 2) < 1e-5 and 0 < samples[1] < 2, "Skin deformation samples failed")
    check(rig.animation_data.action is not None and len(rig.animation_data.action.slots) > 0, "Action slot missing")
    return {"checks": ["edit bone", "weights", "armature modifier", "pose keyframes", "action slot", "three-frame deformation after reopen"], "samples_x": samples, "blend": path}


def render(job):
    empty()
    obj = cube()
    mat = bpy.data.materials.new("橙色")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.14, 0.025, 1)
    obj.data.materials.append(mat)
    camera_data = bpy.data.cameras.new("Camera")
    camera = bpy.data.objects.new("Camera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (5, -6, 4)
    camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera
    light_data = bpy.data.lights.new("Key", "AREA")
    light = bpy.data.objects.new("Key", light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = (3, -4, 6)
    light.rotation_euler = (Vector((0, 0, 0)) - light.location).to_track_quat("-Z", "Y").to_euler()
    light_data.energy = 1000
    light_data.shape = "DISK"
    light_data.size = 4
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.08, .1, .14, 1)
    bpy.context.scene.world = world
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = 8
    s.cycles.use_denoising = False
    s.render.resolution_x = 128
    s.render.resolution_y = 128
    s.render.resolution_percentage = 100
    s.render.image_settings.file_format = "PNG"
    s.render.filepath = str(job / "预览 preview.png")
    path = save_reopen(job)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(job / "预览 preview.png"), check_existing=False)
    check(tuple(image.size) == (128, 128), "Render dimensions wrong")
    pixels = list(image.pixels)
    rgb = [pixels[i] for i in range(len(pixels)) if i % 4 != 3]
    check(max(rgb) - min(rgb) > .1, "Flat or blank render")
    return {"checks": ["camera and light", "Cycles CPU", "render after reopen", "PNG decoded 128x128", "non-flat pixels"], "blend": path, "image": str(job / "预览 preview.png"), "engine": "CYCLES", "device": "CPU"}


def exchange(job):
    empty()
    obj = cube("ExchangeCube")
    obj.location = (1, 2, 3)
    expected = sorted(tuple(obj.matrix_world @ v.co) for v in obj.data.vertices)
    # Update world transform before comparing exported and imported coordinates.
    bpy.context.view_layer.update()
    expected = sorted(tuple(obj.matrix_world @ v.co) for v in obj.data.vertices)
    path = job / "往返 cube.glb"
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True)
    empty()
    bpy.ops.import_scene.gltf(filepath=str(path))
    bpy.context.view_layer.update()
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    check(len(meshes) == 1, "glTF object count mismatch")
    coords = {tuple(round(c, 4) for c in meshes[0].matrix_world @ v.co) for v in meshes[0].data.vertices}
    check(coords == {tuple(round(c, 4) for c in v) for v in expected}, "glTF world positions mismatch")
    return {"checks": ["glTF GLB export", "fresh import", "one mesh", "world coordinates round trip"], "glb": str(path), "unique_vertices": len(coords), "limitations": ["No material/rig/animation round-trip promise"]}


def run(case, job):
    result = globals()[case](job)
    return {"case": case, "status": "proven", "scope": "generated_minimal_fixture_only",
            "blender_version": bpy.app.version_string, **result}
