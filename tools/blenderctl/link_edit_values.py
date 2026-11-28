"""Validate and apply native Object, pose-bone and material slot overrides."""

import math

import bpy
from protocol import Failure


def preflight(owner, values):
    for target, field, value, path in operations(owner, values):
        if not target.is_property_overridable_library(field):
            raise Failure("UNSUPPORTED", "Property is not library-overridable: " + path)
        if field.startswith("rotation_"):
            mode = target.rotation_mode
            required = {"rotation_quaternion": "QUATERNION", "rotation_axis_angle": "AXIS_ANGLE"}
            if (field in required and mode != required[field]
                or field == "rotation_euler" and mode in required.values()):
                raise Failure("UNSUPPORTED", "Requested rotation does not match the existing rotation mode: " + path)
        animation = owner.animation_data
        if animation:
            if animation.nla_tracks:
                raise Failure("UNSUPPORTED", "NLA owners require scene-level review before overriding properties: " + owner.name)
            paths = {curve.data_path for curve in animation.drivers}
            if animation.action and animation.action_slot:
                slot = animation.action_slot.handle
                paths.update(curve.data_path for layer in animation.action.layers for strip in layer.strips
                             for bag in getattr(strip, "channelbags", ()) if bag.slot_handle == slot
                             for curve in bag.fcurves)
            if path in paths:
                raise Failure("CONFLICT", "Local override edit targets an animated or driven property: " + path)


def operations(owner, values):
    for field, value in values.items():
        if field == "pose_bones":
            for spec in value:
                bone = owner.pose.bones.get(spec["name"]) if owner.pose else None
                if bone is None:
                    raise Failure("NOT_FOUND", "Missing pose bone in the specified native override owner: " + spec["name"])
                for prop, component in spec["values"].items():
                    yield bone, prop, component, bone.path_from_id(prop)
        elif field == "material_slots":
            for spec in value:
                index = spec["slot"]
                if index >= len(owner.material_slots):
                    raise Failure("NOT_FOUND", f"Material slot {index} does not exist on {owner.name}")
                slot = owner.material_slots[index]
                material = bpy.data.materials.get(spec["material"]) if spec["material"] else None
                if spec["material"] and (material is None or material.library or material.override_library):
                    raise Failure("NOT_FOUND", "Requested material must exist in the receiving project as a local Material")
                yield slot, "link", "OBJECT", f"material_slots[{index}].link"
                yield slot, "material", material, f"material_slots[{index}].material"
        else:
            yield owner, field, value, field


def apply(owner, values):
    for target, field, expected, _ in operations(owner, values):
        setattr(target, field, expected)


def check(owner, values):
    for target, field, expected, path in operations(owner, values):
        actual = getattr(target, field)
        if isinstance(expected, list):
            matches = len(actual) == len(expected) and all(
                math.isclose(a, b, rel_tol=2e-7, abs_tol=1e-6) for a, b in zip(actual, expected))
        else:
            matches = actual == expected
        if not matches:
            raise Failure("VALIDATION_FAILED", "Local native override value did not survive: " + path)
