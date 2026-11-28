"""Build a local Hair Essentials stack on UV-bound guides and export evaluated cards."""
import hashlib
import json
import math
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


STACK = {
    "Interpolate Hair Curves": {"Density": 160000., "Interpolation Guides": 4, "Viewport Amount": 1.},
    "Clump Hair Curves": {"Factor": 0.18, "Guide Distance": 0.008},
    "Frizz Hair Curves": {"Factor": 0.22, "Distance": 0.0012},
    "Curl Hair Curves": {"Factor": 0.12, "Radius": 0.002, "Subdivision": 0},
    "Trim Hair Curves": {"Replace Length": False, "Length Factor": 0.95},
    "Set Hair Curve Profile": {"Radius": 0.00014, "Shape": 0.65},
}


def installed_library():
    version = ".".join(str(x) for x in bpy.app.version[:2])
    relative = Path("datafiles/assets/nodes/procedural_hair_node_assets.blend")
    paths = [Path(bpy.app.binary_path).parent / version / relative,
             Path(bpy.utils.resource_path("SYSTEM")) / relative]
    return next((p for p in paths if p.is_file()), None)


def assemble(guide, name, settings=None, material=None):
    """Use rest-space interpolation and styling, then deform the finished strands once."""
    if guide.type != "CURVES" or not guide.data.surface or not guide.data.surface_uv_map:
        raise ValueError("Expected Hair Curves with an explicit scalp and UV map")
    if guide.data.surface_uv_map not in guide.data.surface.data.uv_layers:
        raise ValueError("Missing scalp UV map")
    if "surface_uv_coordinate" not in guide.data.attributes or not guide.data.surface.add_rest_position_attribute:
        raise ValueError("Missing curve UV attachments or evaluated scalp rest_position")
    writable = next((c for c in guide.users_collection if c.library is None
                     and c.override_library is None and c.is_editable),None)
    if writable is None or not name or name in bpy.data.objects:
        raise ValueError("A linked scene collection and an unused output name are required")
    custom = settings or {}
    if set(custom) - STACK.keys():
        raise ValueError("Unknown Hair Essentials stage")
    library = installed_library()
    if not library:
        raise FileNotFoundError("Blender Hair Essentials library")
    with bpy.data.libraries.load(str(library), link=False) as (source, target):
        if not set(STACK).issubset(source.node_groups):
            raise ValueError("Required Hair Essentials groups missing from host")
        target.node_groups = list(STACK)
    groups = list(target.node_groups)
    groom = guide.copy()
    groom.data = guide.data.copy()
    for key in list(groom.keys()):
        del groom[key]
    groom.modifiers.clear()
    groom.name = name
    try:
        writable.objects.link(groom)
        groom.hide_render = False
        groom.hide_viewport = False
        groom.hide_set(False)
        for (stage, defaults), group in zip(STACK.items(), groups, strict=True):
            # The shipped panel caps density at 10,000 strands per square scene unit.
            # A metre-scale scalp needs a wider UI range on this appended local copy.
            if stage == "Interpolate Hair Curves":
                density = next(i for i in group.interface.items_tree if i.name == "Density")
                density.max_value = max(density.max_value, 1_000_000.)
            mod = groom.modifiers.new(stage, "NODES")
            mod.node_group = group
            for key, value in (defaults | custom.get(stage, {})).items():
                sockets = [i for i in group.interface.items_tree
                           if i.item_type == "SOCKET" and i.in_out == "INPUT" and i.name == key]
                # Repeated Seed labels on certain assets can be addressed by identifier.
                if len(sockets) != 1:
                    sockets = [i for i in group.interface.items_tree if i.item_type == "SOCKET"
                               and i.in_out == "INPUT" and i.identifier == key]
                if len(sockets) != 1:
                    raise ValueError(f"Unknown or ambiguous {stage} input {key}")
                socket = sockets[0]
                if isinstance(value, (int, float)) and (not math.isfinite(value) or
                        not getattr(socket,"min_value",-math.inf) <= value <= getattr(socket,"max_value",math.inf)):
                    raise ValueError(f"Out of range: {stage}/{key}")
                getattr(mod.properties.inputs, socket.identifier).value = value
        group = bpy.data.node_groups.new(name + " • Surface", "GeometryNodeTree")
        for side in ("INPUT", "OUTPUT"):
            group.interface.new_socket(name="Geometry", in_out=side, socket_type="NodeSocketGeometry")
        start = group.nodes.new("NodeGroupInput")
        deform = group.nodes.new("GeometryNodeDeformCurvesOnSurface")
        end = group.nodes.new("NodeGroupOutput")
        group.links.new(start.outputs[0], deform.inputs["Curves"])
        output = deform.outputs["Curves"]
        if material:
            assign = group.nodes.new("GeometryNodeSetMaterial")
            assign.inputs["Material"].default_value = material
            group.links.new(output, assign.inputs["Geometry"])
            output = assign.outputs["Geometry"]
        group.links.new(output, end.inputs[0])
        groom.modifiers.new("UV Surface Deform", "NODES").node_group = group
        groom["hair_essentials"] = json.dumps({"schema": 1, "guide": guide.name,
            "scalp": guide.data.surface.name, "uv_map": guide.data.surface_uv_map,
            "host_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "settings": {s: d | custom.get(s, {}) for s, d in STACK.items()}})
        bpy.context.view_layer.update()
        return groom
    except Exception:
        data = groom.data
        bpy.data.objects.remove(groom, do_unlink=True)
        bpy.data.hair_curves.remove(data)
        raise


def card_material(name, tint=(.13, .052, .025)):
    """Packed tapered strand texture shared by local previews and glTF exports."""
    width, height = 128, 256
    y, x = np.mgrid[0:height, 0:width]
    u, v = (x + .5) / width, (y + .5) / height
    rng = np.random.default_rng(53)
    alpha = np.zeros_like(u)
    for center in np.linspace(.05, .95, 23):
        end = rng.uniform(.70, 1.02)
        track = .5 + (center - .5) * (1 - .65 * v**2) + .018 * np.sin(6 * v + center * 9)
        alpha = np.maximum(alpha, np.exp(-((u - track) / .020)**2) * np.clip((end-v)*14, 0, 1))
    pixels = np.empty((height,width,4), dtype=np.float32)
    pixels[:,:,:3] = tint
    pixels[:,:,3] = alpha
    image = bpy.data.images.new(name, width=width, height=height, alpha=True)
    image.pixels.foreach_set(pixels.ravel())
    image.pack()
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    mat.node_tree.links.new(tex.outputs["Color"], shader.inputs["Base Color"])
    mat.node_tree.links.new(tex.outputs["Alpha"], shader.inputs["Alpha"])
    return mat


def cards_from_curves(groom, name, *, limit=160, width=.002, material=None):
    """Bake one frame into constant-topology UV strips sampled from evaluated strands."""
    if not isinstance(limit,int) or limit < 1 or not math.isfinite(width) or width <= 0:
        raise ValueError("Invalid card limit or width")
    ev = groom.evaluated_get(bpy.context.evaluated_depsgraph_get())
    geometry = ev.evaluated_geometry()
    curves = geometry.curves
    if not curves or not len(curves.curves) or name in bpy.data.objects:
        raise ValueError("Expected evaluated strands and an unused object name")
    verts, faces, uv_faces = [], [], []
    for index in np.linspace(0, len(curves.curves)-1, min(limit,len(curves.curves)), dtype=int):
        points = [ev.matrix_world @ p.position for p in curves.curves[int(index)].points]
        if len(points)<2:
            raise ValueError("Cannot form a ribbon from one point")
        start = len(verts)
        previous = None
        for i,p in enumerate(points):
            direction = (points[min(i+1,len(points)-1)] - points[max(0,i-1)]).normalized()
            if direction.length_squared < .5:
                raise ValueError("Degenerate evaluated hair tangent")
            axes = (Vector((1,0,0)), Vector((0,1,0)), Vector((0,0,1)))
            side = previous if previous is not None else min(axes,key=lambda a: abs(a.dot(direction)))
            side = (side - direction * side.dot(direction)).normalized()
            if side.length_squared < .5:
                side = min(axes,key=lambda a: abs(a.dot(direction))).cross(direction).normalized()
            previous = side
            t = i / (len(points)-1)
            half = .5 * width * (1-.85*t*t)
            verts.extend((p-side*half,p+side*half))
            if i:
                k = start + 2*i
                faces.append((k-2,k-1,k+1,k))
                old = (i-1)/(len(points)-1)
                uv_faces.append(((0,old),(1,old),(1,t),(0,t)))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts,[],faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="HairCardUV")
    for poly, coords in zip(mesh.polygons,uv_faces,strict=True):
        poly.use_smooth = True
        for loop,coord in zip(poly.loop_indices,coords,strict=True):
            uv.data[loop].uv = coord
    obj = bpy.data.objects.new(name,mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material or card_material(name+" Strand Atlas"))
    return obj
