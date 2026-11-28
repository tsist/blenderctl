"""Explicit used-slot material ink policies for the native Toon joint renderer."""

import copy
import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-line-material"
WRITE = "edit-line-material"
FIELDS = {"source", "group", "categories", "amount", "gain", "tint"}
SOURCE_FIELDS = {"kind", "color", "node", "direction", "socket", "image", "uv_name",
                 "uv_scale", "uv_offset", "filter", "extension"}
CATEGORIES = {"outline", "object", "intersection", "smooth", "material", "selected_edge", "normal_angle", "wireframe"}


def target_value(target):
    require(isinstance(target, dict) and target.keys() == {"object", "slot", "material"}, "INVALID_REQUEST",
            "target requires exact object, slot and material")
    require(all(contract.name(target[key], 1024) for key in ("object", "material")) and
            type(target["slot"]) is int and 0 <= target["slot"] <= 32766,
            "INVALID_REQUEST", "Invalid material-slot identity")
    return dict(target)


def unit(value):
    return type(value) in (int,float) and 0 <= value <= 1


def vector(value, count, check):
    return isinstance(value,list) and len(value)==count and all(check(item) for item in value)


def checked_patch(patch):
    require(isinstance(patch,dict) and patch and patch.keys() <= FIELDS, "INVALID_REQUEST", "Invalid material ink patch")
    if "group" in patch:
        require(patch["group"] is None or contract.name(patch["group"],128), "INVALID_REQUEST", "Invalid line group ID")
    if "categories" in patch:
        cats=patch["categories"]
        require(isinstance(cats,list) and 1<=len(cats)<=len(CATEGORIES) and
                all(isinstance(cat,str) and cat in CATEGORIES for cat in cats) and len(set(cats))==len(cats),
                "INVALID_REQUEST", "Invalid ink categories")
    for key in ("amount","gain"):
        if key in patch:
            require(unit(patch[key]), "INVALID_REQUEST", key+" must be a number in 0..1")
    if "tint" in patch:
        require(vector(patch["tint"],3,unit), "INVALID_REQUEST", "Tint needs three linear RGB values in 0..1")
    if "source" in patch:
        source=patch["source"]
        require(isinstance(source,dict) and source and source.keys() <= SOURCE_FIELDS,
                "INVALID_REQUEST", "Invalid material ink source fields")
        for key, choices in (("kind",("AUTHOR_RGB","NODE_RGBA","UV_IMAGE")),("direction",("INPUT","OUTPUT")),
                             ("filter",("LINEAR","NEAREST")),("extension",("EXTEND","REPEAT"))):
            if key in source:
                require(source[key] in choices,"INVALID_REQUEST","Unknown ink source "+key)
        for key in ("node","socket","image","uv_name"):
            if key in source:
                require(contract.name(source[key],1024),"INVALID_REQUEST","Invalid ink source "+key)
        for key in ("uv_scale","uv_offset"):
            if key in source:
                require(vector(source[key],2,lambda value:type(value) in (int,float) and -65536<=value<=65536),
                        "INVALID_REQUEST", key+" needs two bounded values")
        if "color" in source:
            require(vector(source["color"],3,unit),"INVALID_REQUEST","Author color needs three linear RGB values")
    return dict(patch)


def normalize_request(document,editions):
    operation=document["operation"]
    payload={key:value for key,value in document.items() if key not in {"target","policy_id","draft","material_action"}}
    require(not {"scope","frames","elements","patch"} & payload.keys(),"INVALID_REQUEST","Use explicit material slot identity")
    if operation==READ:
        require("material_action" not in document,"INVALID_REQUEST","Read accepts no updates")
        payload.update(operation="inspect",scope={"groups":["line-material"]})
    else:
        require(not {"policy_id","draft"} & document.keys(),"INVALID_REQUEST","Use material_action for write identity")
        payload["operation"]="render-camera"
    request=contract.normalize_request(payload,editions)
    request.pop("scope",None)
    request["operation"]=operation
    if "target" in document or operation==WRITE:
        request["target"]=target_value(document.get("target"))
    if operation==READ:
        if "policy_id" in document:
            require(contract.name(document["policy_id"],128) and "target" in request,"INVALID_REQUEST",
                    "Policy row inspection requires its exact material slot")
            request["policy_id"]=document["policy_id"]
        if "draft" in document:
            require(document["draft"] is True and "target" in request and "policy_id" not in request,
                    "INVALID_REQUEST","draft=true needs one target and no existing policy ID")
            request["draft"]=True
    else:
        action=document.get("material_action")
        require(isinstance(action,dict) and {"action","id"}<=action.keys() and
                action["action"] in ("ADD","UPDATE","REMOVE") and contract.name(action["id"],128),
                "INVALID_REQUEST","Expected ADD, UPDATE or REMOVE with an exact policy ID")
        fields={"action","id"}
        if action["action"]!="REMOVE":
            fields.add("patch")
        require(action.keys()==fields,"INVALID_REQUEST","Unexpected material policy action fields")
        request["material_action"]=dict(action)
        if "patch" in action:
            request["material_action"]["patch"]=checked_patch(action["patch"])
    return request


def slot(scene,target):
    obj=scene.objects.get(target["object"])
    require(obj is not None and obj.type=="MESH" and obj.library is None and obj.override_library is None,
            "NOT_FOUND","No local scene mesh named "+target["object"])
    index=target["slot"]
    require(index<len(obj.material_slots) and any(face.material_index==index for face in obj.data.polygons),
            "NOT_FOUND","Target material slot is not assigned to a face")
    material=obj.material_slots[index].material
    require(material is not None and material.name_full==target["material"] and material.library is None
            and material.override_library is None,"CONFLICT","Named material differs from the saved used slot")
    return obj,material


def base_policy(identifier,material,core):
    return {"id":identifier,"material":{"name":material.name_full,"library":None},"groups":[],
            "categories":list(core.CATEGORIES),"source":{"kind":"AUTHOR_RGB","color":[.5,.2,.3]},
            "amount":0,"gain":1,"tint":[1,1,1]}


def source_patch(before,change):
    kind=change.get("kind",before["kind"])
    allowed={"AUTHOR_RGB":{"color"},"NODE_RGBA":{"node","direction","socket"},
             "UV_IMAGE":{"image","uv_name","uv_scale","uv_offset","filter","extension"}}[kind]
    require(change.keys()-{"kind"} <= allowed,"INVALID_REQUEST","Source fields do not match "+kind)
    defaults={"AUTHOR_RGB":{"color":[.5,.2,.3]},"NODE_RGBA":{"direction":"INPUT","socket":"Base Color"},
              "UV_IMAGE":{"uv_transform":[1.,0.,0.,0.,1.,0.],"filter":"LINEAR","extension":"EXTEND",
                          "support":"LOWEST_MATCHING_TRIANGLE"}}
    source=dict(before) if kind==before["kind"] else {"kind":kind,**defaults[kind]}
    source.update({key:value for key,value in change.items() if key not in {"uv_scale","uv_offset"}})
    if kind=="UV_IMAGE" and {"uv_scale","uv_offset"} & change.keys():
        a=list(source["uv_transform"])
        if "uv_scale" in change:
            a[0],a[4]=change["uv_scale"]
        if "uv_offset" in change:
            a[2],a[5]=change["uv_offset"]
        source["uv_transform"]=a
    return source


def protected_state(scene,core,dependencies,saved_elements):
    from toon_workflow.toon_workflow_addon import joint_workflow

    author=joint_workflow.capture_author(scene)
    author["document"]["document"]["request"].pop(core.KEY,None)
    return fingerprint({"author":author,"elements":saved_elements(scene.toon_viewport_preview),
                        "geometry":joint_workflow.non_author_signature(scene),"dependencies":dependencies()})


def transaction(scene,ui,core,material,payload,action,known_groups):
    from toon_workflow.toon_viewport_preview import curve_editor

    settings=scene.toon_viewport_preview
    preset=settings.preset_text
    require(scene.library is None and scene.override_library is None and scene.is_editable and preset.library is None
            and preset.get("toon_viewport_preview_owned"),"UNSUPPORTED","Ink edit requires a local owned PRESET")
    tree=settings.curve_tree
    if tree is not None and tree.get("toon_viewport_curve_role")==curve_editor.ROLE:
        require(curve_editor.signature(tree,settings)==tree.get("last_signature"),"CONFLICT",
                "Commit saved brush curve changes before editing ink")
    rows=copy.deepcopy(payload["policies"]) if payload else []
    cmd,identifier=action["action"],action["id"]
    old=next((index for index,item in enumerate(rows) if item["id"]==identifier),None)
    require((old is None)==(cmd=="ADD"),"CONFLICT","Policy ID already exists or has disappeared")
    ref={"name":material.name_full,"library":None}
    if old is not None:
        require(rows[old]["material"]==ref,"CONFLICT","Policy belongs to another used material")
    if cmd=="REMOVE":
        rows.pop(old)
    else:
        before=base_policy(identifier,material,core) if old is None else rows[old]
        patch=action["patch"]
        row={**before,**{key:value for key,value in patch.items() if key not in {"group","source"}}}
        if "group" in patch:
            row["groups"]=[patch["group"]] if patch["group"] else []
        if "source" in patch:
            row["source"]=source_patch(before["source"],patch["source"])
        if old is None:
            rows.append(row)
        else:
            require(row!=before,"CONFLICT","Ink patch is a no-op")
            rows[old]=row
    schema=payload["schema"] if payload else core.SCHEMA
    if any(row["source"]["kind"]=="UV_IMAGE" for row in rows):
        schema=core.SCHEMA_V2
    try:
        candidate=core.normalize({"schema":schema,"policies":rows},known_groups) if rows else None
        return candidate,ui.set_payload(scene,candidate)
    except (ValueError, TypeError, OverflowError) as error:
        raise Failure("CONFLICT",str(error)) from error


def work(bpy,context,request,wrapped,author,*,dependencies,saved_elements,save_candidate,author_sha):
    from toon_workflow.toon_workflow_addon import line_material_colors as core, line_material_ui as ui
    from toon_workflow.toon_viewport_preview import addon

    scene=context.scene
    plan=addon.effective_preset(scene)
    payload=core.records(plan)
    resolved=core.resolve(scene,plan)
    if resolved is not None:
        core.validate_receipt(resolved)
    protection=protected_state(scene,core,dependencies,saved_elements)
    response={"operation":request["operation"],"product":request["product"],"context":request["context"],
        "source":{"file":request["file"],"sha256":request["expected_sha256"],"unchanged":True},
        "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    target=request.get("target")
    if target is not None:
        obj,material=slot(scene,target)
        response["target"]=target
    if request["operation"]==READ:
        state_sha=fingerprint([payload,resolved])
        if "verify_state_sha256" in request:
            require(state_sha==request["verify_state_sha256"] and protection==request["verify_scene_sha256"],
                    "VALIDATION_FAILED","Cold reopen changed ink or protected scene state")
        if target is not None:
            candidates=[row for row in (payload or {}).get("policies",[]) if row["material"]["name"]==material.name_full]
            response["target_policies"]=candidates
            if "policy_id" in request:
                policy=next((row for row in candidates if row["id"]==request["policy_id"]),None)
                require(policy is not None,"NOT_FOUND","Policy is absent from the named used material")
                response["policy"]=policy
            if request.get("draft"):
                index=obj.active_material_index
                try:
                    obj.active_material_index=target["slot"]
                    with context.temp_override(active_object=obj,object=obj):
                        ui.read_current(context,new=True)
                    response["draft"]=base_policy(scene.toon_line_material_ui.policy_id,material,core)
                finally:
                    obj.active_material_index=index
        require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Source changed during ink inspection")
        return {**response,"policies":payload,"resolved":resolved,"author_sha256":author,
                "state_sha256":state_sha,"scene_sha256":protection}

    candidate,native=transaction(scene,ui,core,material,payload,request["material_action"],[row["id"] for row in plan["groups"]])
    after_plan=addon.effective_preset(scene)
    after=core.records(after_plan)
    after_resolved=core.resolve(scene,after_plan)
    require(after==candidate and native==after_resolved,"VALIDATION_FAILED","Saved ink differs from the approved policy")
    require(protected_state(scene,core,dependencies,saved_elements)==protection,"VALIDATION_FAILED",
            "Ink edit changed protected scene or author state")
    if after_resolved is not None:
        core.validate_receipt(after_resolved)
    updated=author_sha(scene)
    retained=save_candidate(bpy,request)
    return {**response,"policies":after,"resolved":after_resolved,"material_event":request["material_action"]["action"],
            "author_before_sha256":author,"author_after_sha256":updated,"state_sha256":fingerprint([after,after_resolved]),
            "scene_sha256":protection,"candidate_sha256":digest(request["stage"]),"retained_orphans":retained,
            "protected":{"status":"passed","non_ink_author":True}}
