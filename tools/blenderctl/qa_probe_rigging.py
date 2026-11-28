"""Read-only rigging discipline checks for saved assets."""

from qa import finding


def run_probe(context, profile):
    findings = []
    mesh_qa = context["mesh_qa"]
    for entry in [*mesh_qa["hard_blockers"], *mesh_qa["review_signals"]]:
        if entry["code"] in {"unweighted_verts", "unnormalized_weights", "exceed_bone_influences"}:
            findings.append(finding(entry["code"], "G2", count=entry["count"], examples=entry["witnesses"]))
    items = {}
    for obj in context["scene"].objects:
        if obj.type == "ARMATURE" or any(mod.type == "ARMATURE" for mod in obj.modifiers):
            items[obj.as_pointer()] = obj
            if obj.data:
                items[obj.data.as_pointer()] = obj.data
            if obj.type == "MESH" and obj.data.shape_keys:
                keys = obj.data.shape_keys
                items[keys.as_pointer()] = keys
    for item in items.values():
        animation = item.animation_data
        if animation is None:
            continue
        for curve in animation.drivers:
            issues = []
            if not curve.is_valid or not curve.driver.is_valid:
                issues.append("invalid_fcurve")
            for var in curve.driver.variables:
                if var.type == "CONTEXT_PROP":
                    continue
                for target in var.targets:
                    if target.id is None:
                        if not target.use_fallback_value:
                            issues.append(f"missing target for {var.name}")
                        continue
                    if var.type == "SINGLE_PROP":
                        try:
                            target.id.path_resolve(target.data_path)
                        except (AttributeError, ValueError):
                            if not target.use_fallback_value:
                                issues.append(f"invalid path for {var.name}")
                    if target.bone_target and (
                        not getattr(target.id, "pose", None) or target.bone_target not in target.id.pose.bones
                    ):
                        issues.append(f"missing bone for {var.name}")
            if issues:
                findings.append(finding("broken_driver", "G1", owner=item.name, path=curve.data_path,
                                        array_index=curve.array_index, issues=issues))
    return findings
