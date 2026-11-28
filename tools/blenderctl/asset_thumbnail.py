"""Generate embedded-preview inputs from the actual asset in a disposable scene."""

import math

import bpy
from mathutils import Vector
from protocol import Failure, atomic_json, digest

SIZE = 128


def render(scene, filename):
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.resolution_x = SIZE
    scene.render.resolution_y = SIZE
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = str(filename)
    scene.view_settings.view_transform = "AgX"
    if bpy.ops.render.render(write_still=True, scene=scene.name) != {"FINISHED"} or not filename.is_file():
        raise Failure("VALIDATION_FAILED", "Blender did not produce the asset thumbnail")


def preview_geometry(item, filename):
    import asset_previews
    import preview_framing

    kind = item.bl_rna.identifier
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    spec = {
        "fixture": "NONE" if kind in {"Object", "Collection"} else "UV_SPHERE",
        "visibility": "REQUIRE_VISIBLE", "appearance": "SOURCE",
        "frame": scene.frame_current,
        "width": SIZE, "height": SIZE, "background": (.13, .17, .23),
        "engine": "CYCLES", "samples": 4,
    }
    selected = asset_previews.targets(spec, item, scene, layer)
    if bpy.data.libraries:
        selected = asset_previews.localize_archived_instances(selected)
    layer.update()
    frozen, positions = asset_previews.freeze_meshes(selected, spec)
    studio, camera, points, _ = asset_previews.studio(frozen, positions, spec)
    preview_framing.fit(studio, camera, points, "THREE_QUARTER", "ORTHO", .13)
    studio.camera = camera
    render(studio, filename)


def preview_world(item, filename):
    scene = bpy.data.scenes.new("Thumbnail World")
    scene.world = item
    camera = bpy.data.objects.new("HDRI camera", bpy.data.cameras.new("HDRI camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.lens = 16
    # A lit neutral sphere shows the stored world lighting as well as its background.
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=.5, location=(0, 2, 0))
        sphere = bpy.context.object
        for face in sphere.data.polygons:
            face.use_smooth = True
    camera.rotation_euler = Vector((0, 1, 0)).to_track_quat("-Z", "Y").to_euler()
    render(scene, filename)


def line(pixels, start, end, color):
    count = max(1, int(math.dist(start, end)))
    for index in range(count + 1):
        x, y = (round(a + (b - a) * index / count) for a, b in zip(start, end))
        pixels[max(0, y - 1):min(SIZE, y + 2), max(0, x - 1):min(SIZE, x + 2)] = color


def preview_diagram(item, filename):
    import numpy as np

    pixels = np.empty((SIZE, SIZE, 4), dtype=np.float32)
    pixels[:] = (.025, .041, .065, 1)
    for axis in range(12, SIZE, 16):
        pixels[axis, :, :3] = (.045, .070, .092)
        pixels[:, axis, :3] = (.045, .070, .092)
    if isinstance(item, bpy.types.Action):
        colors = ((.05, .65, .8, 1), (.9, .3, .1, 1), (.32, .9, .34, 1))
        curves = [curve for layer in item.layers for strip in layer.strips
                  for bag in getattr(strip, "channelbags", ()) for curve in bag.fcurves if len(curve.keyframe_points)]
        if not curves:
            raise Failure("VALIDATION_FAILED", "Action has no keyed channels to thumbnail")
        start, end = item.frame_range
        for index, curve in enumerate(curves):
            samples = [curve.evaluate(start + (end - start) * t / 99) for t in range(100)]
            low, high = min(samples), max(samples)
            points = [(14 + offset, 28 + 72 * (height - low) / (high - low) if high != low else 28 + 8 * (index % 4))
                      for offset, height in enumerate(samples)]
            for first, second in zip(points, points[1:]):
                line(pixels, first, second, colors[index % len(colors)])
    else:
        nodes = list(item.nodes)
        if not nodes:
            raise Failure("VALIDATION_FAILED", "Compositor group has no nodes to thumbnail")
        coords = {node: node.location_absolute for node in nodes}
        lo_x, hi_x = min(p.x for p in coords.values()), max(p.x for p in coords.values())
        lo_y, hi_y = min(p.y for p in coords.values()), max(p.y for p in coords.values())
        points = {n: (round(22 + 82 * (p.x - lo_x) / max(1, hi_x - lo_x)),
                      round(55 + 54 * (p.y - lo_y) / max(1, hi_y - lo_y))) for n, p in coords.items()}
        for link in item.links:
            line(pixels, points[link.from_node], points[link.to_node], (.12, .56, .79, 1))
        for x, y in points.values():
            pixels[y-8:y+8, x-12:x+12] = (.14, .29, .41, 1)
            pixels[y+4:y+8, x-12:x+12] = (.40, .67, .81, 1)
    image = bpy.data.images.new("Asset Thumbnail", width=SIZE, height=SIZE, alpha=False)
    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = str(filename)
    image.file_format = "PNG"
    image.save()


def generate(file, specs, job):
    from inspection import all_ids
    from verification import resolve

    results = []
    for index, spec in enumerate(specs):
        if spec.get("preview") != {"automatic": True}:
            continue
        bpy.ops.wm.open_mainfile(filepath=file, load_ui=False, use_scripts=False)
        item = resolve(list(all_ids()), spec["selector"])
        if item.library or item.override_library:
            raise Failure("UNSUPPORTED", "Generate an automatic thumbnail from the local source asset")
        kind = item.bl_rna.identifier
        filename = job / f"asset-thumbnail-{index + 1:02d}.png"
        if kind == "World":
            preview_world(item, filename)
        elif kind in {"Action", "CompositorNodeTree"}:
            preview_diagram(item, filename)
        else:
            preview_geometry(item, filename)
        spec["preview"] = {"file": str(filename), "expected_sha256": digest(filename)}
        results.append({"target": spec["selector"], "type": kind, **spec["preview"]})
    if results:
        bpy.ops.wm.open_mainfile(filepath=file, load_ui=False, use_scripts=False)
        atomic_json(job / "asset-thumbnails.json", results)
    return results
