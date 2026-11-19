"""Auxiliary non-rendering UV2 lightmap generation and validation."""
import bpy
import numpy as np

from .contract import fail


def _restore_render_flags(mesh, prev_active, prev_render, layer_name):
    for name in prev_render:
        if name in mesh.uv_layers:
            mesh.uv_layers[name].active_render = True
            break
    if layer_name in mesh.uv_layers:
        mesh.uv_layers[layer_name].active_render = False
    if prev_active and prev_active in mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[prev_active]


def generate_lightmap(scene, objects, s, v, unit, progress):
    """Create or overwrite an auxiliary non-active_render lightmap UV layer safely."""
    from . import core
    from .consumers import inspect_objects
    from .metrics import measure

    spec = v["lightmap"]
    layer_name = spec["layer"]
    width = spec["width"]
    height = spec["height"]
    box_div = spec["box_div"]
    margin_div = spec["margin_div"]
    overwrite = spec["overwrite"]

    for obj in objects:
        if obj.data.users != 1:
            fail("Shared mesh needs an explicit isolated working copy: " + obj.name, "UNSUPPORTED")
        if not obj.data.uv_layers or not any(u.active_render for u in obj.data.uv_layers):
            fail("Auxiliary lightmap requires an existing primary active_render UV layer first: " + obj.name, "CONFLICT")
        if any(
            rec.get("layer") == layer_name and obj.name in rec.get("objects", ())
            for rec in s.get("candidates", {}).values()
        ):
            fail("Refusing to overwrite managed candidate layer as auxiliary lightmap: " + layer_name, "CONFLICT")
        if layer_name in obj.data.uv_layers:
            if not overwrite:
                fail("Lightmap UV layer already exists; set overwrite=true to update: " + obj.name + "/" + layer_name, "CONFLICT")
            if obj.data.uv_layers[layer_name].active_render:
                fail("Refusing to overwrite active_render UV layer as auxiliary lightmap: " + obj.name + "/" + layer_name, "CONFLICT")
        elif len(obj.data.uv_layers) >= 8:
            fail("Not enough UV layer slots for lightmap: " + obj.name, "RESOURCE_LIMIT")

    consumers = inspect_objects(objects)
    for obj in objects:
        info = consumers[obj.name]
        if layer_name in info.get("protected_udim_layers", ()):
            fail("Target layer is bound to a UDIM consumer: " + obj.name + "/" + layer_name, "UNSUPPORTED")

    core._check_geometry(objects, unit)

    before_protection = core.protection(objects, excluded=(layer_name,))
    backups = {}
    for obj in objects:
        mesh = obj.data
        existed = layer_name in mesh.uv_layers
        prev_uv = core.coordinates(mesh, layer_name).copy() if existed else None
        prev_pins = core._pins(mesh, layer_name) if existed else []
        backups[obj.name] = {
            "existed": existed,
            "uv": prev_uv,
            "pins": prev_pins,
            "active": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
            "render": [u.name for u in mesh.uv_layers if u.active_render],
        }

    progress("solve", method="LIGHTMAP_PACK")
    with core.scratch(objects) as (_, clones):
        for clone in clones:
            mesh = clone.data
            while mesh.uv_layers:
                mesh.uv_layers.remove(mesh.uv_layers[0])
            mesh.uv_layers.new(name=layer_name, do_init=False)
        core._edit(clones)
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            outcome = bpy.ops.uv.lightmap_pack(
                PREF_CONTEXT="ALL_FACES",
                PREF_PACK_IN_ONE=True,
                PREF_NEW_UVLAYER=False,
                PREF_BOX_DIV=box_div,
                PREF_MARGIN_DIV=margin_div,
            )
            if outcome != {"FINISHED"}:
                fail("Native lightmap_pack did not finish: " + str(outcome), "VALIDATION_FAILED")
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        arrays = [core.coordinates(clone.data, layer_name) for clone in clones]

    try:
        for obj, uv in zip(objects, arrays):
            mesh = obj.data
            if layer_name not in mesh.uv_layers:
                u = mesh.uv_layers.new(name=layer_name, do_init=False)
                if u.name != layer_name:
                    fail("Lightmap UV layer name collision: " + u.name, "CONFLICT")
            core._write_uv(mesh, layer_name, uv)
            pin_col = mesh.uv_layers[layer_name].pin
            if len(pin_col) == len(mesh.loops):
                pin_col.foreach_set("value", np.zeros(len(mesh.loops), dtype=bool))
            b = backups[obj.name]
            _restore_render_flags(mesh, b["active"], b["render"], layer_name)
            if mesh.uv_layers[layer_name].active_render:
                fail("Failed to keep auxiliary lightmap layer non-rendering: " + obj.name, "VALIDATION_FAILED")
            if [u.name for u in mesh.uv_layers if u.active_render] != b["render"]:
                fail("Active render UV bindings changed unexpectedly on " + obj.name, "VALIDATION_FAILED")

        if core.protection(objects, excluded=(layer_name,)) != before_protection:
            fail("Original data protection mismatch during lightmap generation", "VALIDATION_FAILED")

        q = measure(
            core.measurement_input(objects, layer_name, unit),
            width=width,
            height=height,
            pair_budget=v["pair_budget"],
            cancel=lambda: progress("checking"),
            usage={"mode": "EXCLUSIVE", "reuse_groups": []},
        )
        all_uv = np.concatenate(arrays)
        uv_min = [float(x) for x in all_uv.min(axis=0)]
        uv_max = [float(x) for x in all_uv.max(axis=0)]
        within_unit_tile = bool(all(-1e-6 <= val <= 1.0 + 1e-6 for val in (*uv_min, *uv_max)))
        if not q.get("overlap", {}).get("complete", False) or q["overlap"]["positive_pairs"] != 0:
            fail(
                "Lightmap UV2 contains overlapping islands (" + str(q.get("overlap", {}).get("positive_pairs")) + " pairs)",
                "VALIDATION_FAILED",
            )
        if not within_unit_tile:
            fail("Lightmap UV2 exceeds normalized [0, 1] tile bounds", "VALIDATION_FAILED")
        if q["status"] != "pass":
            fail(
                "Lightmap UV2 failed complete numeric validation: " + q["status"],
                "VALIDATION_FAILED",
            )

        return {
            "status": "lightmap_ready",
            "layer": layer_name,
            "objects": [o.name for o in objects],
            "active_render_preserved": True,
            "overwrote_existing": any(b["existed"] for b in backups.values()),
            "bounds": {"min": uv_min, "max": uv_max, "within_unit_tile": within_unit_tile},
            "lightmap": {
                "layer": layer_name,
                "width": width,
                "height": height,
                "box_div": box_div,
                "margin_div": margin_div,
                "overlap_pairs": q["overlap"]["positive_pairs"],
                "overlap_complete": q["overlap"]["complete"],
                "invalid_uv_triangles": q["invalid_uv_triangles"],
                "out_of_bounds_corners": q["out_of_bounds_corners"],
                "boundary_margin_px": q["boundary_margin_px"],
                "gap_min_px": q["gap"]["min_px"],
                "metrics_status": q["status"],
                "render_bindings": {o.name: backups[o.name]["render"] for o in objects},
                "active_layers": {o.name: backups[o.name]["active"] for o in objects},
            },
            "metrics": q,
            "quality_acceptance": "auxiliary_lightmap_requires_bake_review",
        }
    except Exception:
        for obj in objects:
            mesh = obj.data
            b = backups[obj.name]
            if b["existed"]:
                if layer_name in mesh.uv_layers:
                    core._write_uv(mesh, layer_name, b["uv"])
                    pin_col = mesh.uv_layers[layer_name].pin
                    if len(pin_col) == len(mesh.loops):
                        pin_col.foreach_set("value", np.zeros(len(mesh.loops), dtype=bool))
                    for idx in b["pins"]:
                        mesh.uv_layers[layer_name].data[int(idx)].pin_uv = True
            elif layer_name in mesh.uv_layers:
                mesh.uv_layers.remove(mesh.uv_layers[layer_name])
            _restore_render_flags(mesh, b["active"], b["render"], layer_name)
        raise
