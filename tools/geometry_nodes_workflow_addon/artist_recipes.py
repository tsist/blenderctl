"""Artist presets built on the managed geometry graph contract."""

from pathlib import Path

import bpy

from . import core, recipes


HAIR_ASSET = "Generate Hair Curves"


def load_hair_generator():
    for group in bpy.data.node_groups:
        if group.get("gnw_builtin_asset") == HAIR_ASSET:
            return group
    source = Path(bpy.utils.system_resource("DATAFILES")) / "assets/nodes/procedural_hair_node_assets.blend"
    if not source.is_file():
        raise ValueError("当前 Blender 安装缺少内置毛发节点素材")
    with bpy.data.libraries.load(str(source), link=False) as (available, appended):
        if HAIR_ASSET not in available.node_groups:
            raise ValueError("当前 Blender 安装缺少毛发生成节点组")
        appended.node_groups = [HAIR_ASSET]
    group, = appended.node_groups
    group.interface.items_tree["Surface Source"].default_value = "Object"
    group["gnw_builtin_asset"] = HAIR_ASSET
    return group


def hair_graph(generator):
    graph = recipes._Graph("hair_curves", {
        "hair_length": 0.18, "hair_density": 200.0, "hair_points": 6,
        "hair_radius": 0.002, "card_width": 0.012, "seed": 17,
    })
    graph.socket("hair_surface", "hair_surface", "INPUT", "NodeSocketObject")
    graph.socket("hair_material", "hair_material", "INPUT", "NodeSocketMaterial")
    graph.socket("hair_cards", "hair_cards", "INPUT", "NodeSocketBool", False)
    length = graph.parameter("hair_length", 0.001, 10)
    density = graph.parameter("hair_density", 0, 100000, "per_m2")
    points = graph.parameter("hair_points", 2, 64, "count")
    radius = graph.parameter("hair_radius", 0.0001, 0.1)
    width = graph.parameter("card_width", 0.0001, 1)
    seed = graph.parameter("seed", 0, 1000000, "seed")
    graph.node("generate_hair", "GeometryNodeGroup", {
        "Surface": ("in", "hair_surface"), "Hair Material": ("in", "hair_material"),
        "Hair Length": length, "Control Points": points, "Density": density, "Seed": seed,
    })
    graph.data["nodes"][-1]["resources"] = {"node_tree": core.plain(generator)}
    graph.node("hair_radius", "GeometryNodeSetCurveRadius", {"Curve": ("generate_hair", "Curves"), "Radius": radius})
    half_width = graph.math("half_width", "MULTIPLY", width, 0.5)
    negative = graph.math("negative_width", "MULTIPLY", half_width, -1.0)
    graph.node("ribbon_profile", "GeometryNodeCurvePrimitiveLine", {
        "Start": graph.xyz("left_edge", x=negative), "End": graph.xyz("right_edge", x=half_width),
    }, {"mode": "POINTS"})
    graph.node("ribbons", "GeometryNodeCurveToMesh", {
        "Curve": ("generate_hair", "Curves"), "Profile Curve": ("ribbon_profile", "Curve"),
    })
    graph.node("card_material", "GeometryNodeSetMaterial", {
        "Geometry": ("ribbons", "Mesh"), "Material": ("in", "hair_material"),
    })
    graph.node("curves_or_cards", "GeometryNodeSwitch", {
        "Switch": ("in", "hair_cards"), "False": ("hair_radius", "Curve"),
        "True": ("card_material", "Geometry"),
    }, {"input_type": "GEOMETRY"})
    return graph.output(("curves_or_cards", "Output"))


def simulation_graph():
    graph = recipes._Graph("simulation_translate", {"speed": 0.5})
    speed = graph.parameter("speed", 0, 20, "m/s")
    graph.node("sim_start", "GeometryNodeSimulationInput", {"Geometry": ("in", "Geometry")})
    graph.node("sim_end", "GeometryNodeSimulationOutput")
    graph.data["simulation_zones"] = [{
        "input": "sim_start", "output": "sim_end", "items": [{"name": "Geometry", "type": "GEOMETRY"}],
    }]
    distance = graph.math("displacement", "MULTIPLY", speed, ("sim_start", "Delta Time"))
    moved = graph.transform("step", ("sim_start", "Geometry"), graph.xyz("offset", z=distance))
    graph.link(moved, ("sim_end", "Geometry"))
    return graph.output(("sim_end", "Geometry"))


def biome_graph():
    graph = recipes.build_recipe("biome_scatter")
    graph["interface"].append({
        "key": "align_normals", "name": "align_normals", "direction": "INPUT",
        "type": "NodeSocketBool", "default": True,
    })
    graph["nodes"].append({
        "id": "normal_alignment", "type": "GeometryNodeSwitch",
        "properties": {"input_type": "ROTATION"}, "location": [1050, -1740],
    })
    link = next(row for row in graph["links"] if row["to"] == ["scatter_instances", "Rotation"])
    link["from"] = ["normal_alignment", "Output"]
    graph["links"].extend((
        {"from": ["in", "align_normals"], "to": ["normal_alignment", "Switch"]},
        {"from": ["local_yaw", "Rotation"], "to": ["normal_alignment", "False"]},
        {"from": ["align_to_surface", "Rotation"], "to": ["normal_alignment", "True"]},
    ))
    return graph
