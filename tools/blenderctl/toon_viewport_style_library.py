"""Replay native Toon Style Set authoring and exact used-slot assignments."""

from contextlib import contextmanager
import hashlib
import json
import os
import uuid

from material_draft_handoff import fingerprint, guard_file, require
from protocol import Failure, digest
from toon_viewport_line_template import template_ref
import toon_viewport_lines_contract as contract


READ="inspect-style-set"
EDIT="edit-style-set"
EXPORT="export-style-set"
OPERATIONS=(READ,EDIT,EXPORT)
ACTIONS={
    "NEW":{"name"},"RECORD":{"role","role_action","target"},"BIND":{"role","target"},
    "CAMERA":{"role","camera_role","camera","target"},"BIND_CAMERA":{"camera_role","camera"},
    "IMPORT":{"package"},"APPLY":set(),"RESTORE":set(),"REMOVE_BINDING":{"role","target"},
    "REMOVE_CAMERA":{"camera_role","camera"},"SELECT_CAMERA":{"camera"},
}


def target_ref(value):
    require(isinstance(value,dict) and value.keys()=={"object","slot","material"},"INVALID_REQUEST",
            "Expected object, slot and material name (or null for an empty named slot)")
    require(contract.name(value["object"],1024) and (value["material"] is None or contract.name(value["material"],1024))
            and type(value["slot"]) is int and 0<=value["slot"]<=32766,
            "INVALID_REQUEST","Invalid explicit Style Set slot identity")
    return dict(value)


def normalize_request(document,editions):
    operation=document["operation"]
    payload={key:value for key,value in document.items() if key not in {"set_action","expected_set"}}
    require(not {"scope","frames","elements","patch"} & payload.keys(),"INVALID_REQUEST","Style Set works on named slots")
    if operation==EDIT:
        payload["operation"]="render-camera"
    else:
        require("set_action" not in document,"INVALID_REQUEST","Read and export accept no style edits")
        payload.update(operation="inspect",scope={"groups":["style-set"]})
    result=contract.normalize_request(payload,editions)
    result.pop("scope",None)
    result["operation"]=operation
    if "expected_set" in document:
        version=document["expected_set"]
        require(isinstance(version,dict) and version.keys()=={"id","revision"} and contract.name(version["id"],128)
                and type(version["revision"]) is int and version["revision"]>=1,
                "INVALID_REQUEST","expected_set requires exact id and positive revision")
        result["expected_set"]=version
    if operation!=EDIT:
        return result
    action=document.get("set_action")
    require(isinstance(action,dict) and isinstance(action.get("action"),str) and action["action"] in ACTIONS,
            "INVALID_REQUEST","Unknown Style Set action")
    require(action.keys()==ACTIONS[action["action"]]|{"action"},"INVALID_REQUEST","Unexpected Style Set action fields")
    for key in ("name","role","camera_role","camera"):
        if key in action:
            require(contract.name(action[key],128),"INVALID_REQUEST","Invalid Style Set "+key)
    if "role_action" in action:
        require(action["role_action"] in ("COMPONENT","PRESERVE"),"INVALID_REQUEST","Invalid role action")
    action=dict(action)
    if "target" in action:
        action["target"]=target_ref(action["target"])
    if "package" in action:
        action["package"]=template_ref(action["package"])
    result["set_action"]=action
    return result


def guard_inputs(request,guards):
    if "package" in request.get("set_action",{}):
        data=request["set_action"]["package"]
        data["file"]=str(guard_file(data["file"],guards,data["sha256"]))


def check_version(before,request):
    if "expected_set" in request:
        doc=before["style"]["document"] if before["style"] else None
        version={"id":doc["id"],"revision":doc["revision"]} if doc else None
        require(version==request["expected_set"],"CONFLICT","Saved Style Set id or revision differs")


def saved_state(bpy,context,ui,slots):
    scene=context.scene
    records={text.name:hashlib.sha256(text.as_string().encode("utf8")).hexdigest()
             for text in bpy.data.texts if text.get("toon_style_set_author_archive")}
    style=ui.snapshot(scene)
    bindings=[]
    for row in ui._settings(context).bindings:
        if row.target is None:
            continue
        slot=row.target.material_slots[row.slot] if row.slot<len(row.target.material_slots) else None
        material=slot.material if slot is not None else None
        result={"object":row.target.name,"slot":row.slot,"role":row.role,
                "material":material.name if material else None,"link":slot.link if slot is not None else None}
        if material is not None and slots.KEY in material:
            result["component"]=slots.inspect(scene,row.target,row.slot)
        bindings.append(result)
    return {"style":style,"slot_state":bindings,"camera":scene.camera.name if scene.camera else None,"archives":records}


def named_slot(scene,slots,target):
    obj=scene.objects.get(target["object"])
    require(obj is not None,"NOT_FOUND","Unknown local mesh: "+target["object"])
    slot=slots._slot(scene,obj,target["slot"])
    material=slot.material.name if slot.material else None
    require(material==target["material"],"CONFLICT","Saved used-slot material does not match target")
    return obj,target["slot"]


@contextmanager
def active_slot(context,obj,index):
    prior=obj.active_material_index
    try:
        obj.active_material_index=index
        with context.temp_override(active_object=obj,object=obj):
            yield
    finally:
        obj.active_material_index=prior


def archive_existing(context,ui):
    from toon_workflow.toon_workflow_addon import cel_ui

    old=ui.snapshot(context.scene)
    if old is not None:
        text=cel_ui._archive_text("Toon previous Style Set "+uuid.uuid4().hex,json.dumps(old,ensure_ascii=False))
        text["toon_style_set_author_archive"]="toon-style-set.v1"
        text.use_fake_user=True


def named_camera(context,ui,name):
    return ui._camera(context.scene,context.scene.objects.get(name))


def set_camera_binding(context,ui,settings,role,camera):
    require(any(row["camera_role"]==role for row in ui._doc(context)["camera_overrides"]),"NOT_FOUND",
            "Unknown declared camera role: "+role)
    require(all(row.camera!=camera or row.camera_role==role for row in settings.camera_bindings),"CONFLICT",
            "A camera already belongs to another role")
    existing=next((row for row in settings.camera_bindings if row.camera_role==role),None)
    if existing is None:
        existing=settings.camera_bindings.add()
        existing.camera_role=role
    existing.camera=camera


def changed_slots(context,ui):
    doc=ui._doc(context)
    return {(row.target.name,row.slot) for row in ui._settings(context).bindings
            if row.target is not None and doc["roles"].get(row.role,{}).get("action")=="COMPONENT"}


def transact(context,ui,slots,step):
    cmd=step["action"]
    scene=context.scene
    settings=ui._settings(context)
    if cmd=="NEW":
        archive_existing(context,ui)
        settings.template_name=step["name"]
        return ui.new_current(context)
    if cmd=="IMPORT":
        archive_existing(context,ui)
        package=step["package"]
        require(digest(package["file"])==package["sha256"],"CONFLICT","Style Set package changed")
        settings.json_path=package["file"]
        return ui.import_current(context)
    if cmd=="SELECT_CAMERA":
        scene.camera=named_camera(context,ui,step["camera"])
        return scene.camera.name
    if cmd=="APPLY":
        return ui.apply_current(context)
    if cmd=="RESTORE":
        return ui.restore_current(context)
    if cmd=="BIND_CAMERA":
        set_camera_binding(context,ui,settings,step["camera_role"],named_camera(context,ui,step["camera"]))
        return step["camera_role"]
    if cmd=="REMOVE_CAMERA":
        camera=named_camera(context,ui,step["camera"])
        index=next((i for i,row in enumerate(settings.camera_bindings)
                    if row.camera_role==step["camera_role"] and row.camera==camera),None)
        require(index is not None,"NOT_FOUND","Unknown explicit camera binding")
        settings.camera_bindings.remove(index)
        return step["camera_role"]

    obj,index=named_slot(scene,slots,step["target"])
    role=step["role"]
    if cmd=="REMOVE_BINDING":
        row_index=next((i for i,row in enumerate(settings.bindings) if row.role==role and row.target==obj and row.slot==index),None)
        require(row_index is not None,"NOT_FOUND","Unknown explicit Style Set slot binding")
        settings.bindings.remove(row_index)
        return role
    settings.role_name=role
    with active_slot(context,obj,index):
        if cmd=="RECORD":
            settings.role_action=step["role_action"]
            return ui.record_role_current(context)
        if cmd=="CAMERA":
            settings.camera_role=step["camera_role"]
            settings.camera=named_camera(context,ui,step["camera"])
            return ui.record_camera_current(context)
        bucket=next((row for row in settings.bindings if row.role==role and row.target is None),None)
        if bucket is not None:
            bucket.target,bucket.slot=obj,index
        return ui.bind_current(context)


def protected_scene(scene,affected,read_bindings,dependencies):
    from toon_workflow.toon_workflow_addon import joint_workflow

    author=joint_workflow.capture_author(scene)
    author.pop("style_set",None)
    author.pop("components",None)
    author.pop("schema",None)
    rows=read_bindings()
    for row in rows:
        for index,slot in enumerate(row["materials"]):
            if (row["object"],index) in affected:
                slot[1]=None
    return fingerprint({"author":author,"slots":rows,"dependencies":dependencies(),
                        "meshes":[(obj.name,obj.data.name,len(obj.data.vertices),len(obj.data.polygons))
                                  for obj in sorted(scene.objects,key=lambda obj:obj.name) if obj.type=="MESH"]})


def work(bpy,context,request,job,author,*,save_candidate,author_sha,dependencies,read_bindings):
    from toon_workflow.toon_workflow_addon import component_catalog, component_slots as slots, style_set, style_set_ui as ui

    scene=context.scene
    operation=request["operation"]
    response={"operation":operation,"product":request["product"],"context":request["context"],
        "source":{"file":request["file"],"sha256":request["expected_sha256"],"unchanged":True},
        "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    before=saved_state(bpy,context,ui,slots)
    check_version(before,request)
    full_guard=lambda:protected_scene(scene,set(),read_bindings,dependencies)
    try:
        if operation==READ:
            state_sha=fingerprint(before)
            scene_sha=full_guard()
            if "verify_state_sha256" in request:
                require(state_sha==request["verify_state_sha256"] and scene_sha==request["verify_scene_sha256"],
                        "VALIDATION_FAILED","Cold reopen changed the Style Set record or scene")
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Source changed during style inspection")
            return {**response,"style_set":before,"author_sha256":author,"state_sha256":state_sha,"scene_sha256":scene_sha}
        if operation==EXPORT:
            document=ui._doc(context)
            filename=job/"style-set.json"
            style_set.export_file(document,filename,component_catalog.normalize)
            require(style_set.load_file(filename,component_catalog.normalize)==document,"VALIDATION_FAILED",
                    "Exported Style Set could not reopen")
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Source changed during style export")
            return {**response,"style_set":before,"package":{"file":str(filename),"sha256":digest(filename)},"author_sha256":author}

        require(scene.library is None and scene.override_library is None and scene.is_editable,"UNSUPPORTED","Style editing needs a local scene")
        step=request["set_action"]
        affected=changed_slots(context,ui) if step["action"] in ("APPLY","RESTORE") else set()
        guard=protected_scene(scene,affected,read_bindings,dependencies)
        event=transact(context,ui,slots,step)
        after=saved_state(bpy,context,ui,slots)
        require(before!=after,"CONFLICT","Style Set action did not change the saved state")
        require(guard==protected_scene(scene,affected,read_bindings,dependencies),"VALIDATION_FAILED",
                "Style Set edit changed non-target materials, line groups or dependencies")
        if step["action"]=="APPLY":
            changed={(row["object"],row["slot"]):row.get("component",{}).get("status") for row in after["slot_state"]}
            require(all(changed.get(key)=="valid" for key in affected),"VALIDATION_FAILED","Applied component did not read back")
        if step["action"]=="RESTORE":
            require(not any("component" in row and (row["object"],row["slot"]) in affected for row in after["slot_state"]),
                    "VALIDATION_FAILED","Restored slot still contains a Style Set component")
    except (ValueError,TypeError,KeyError) as error:
        raise Failure("CONFLICT",str(error)) from error
    updated=author_sha(scene)
    scene_sha=full_guard()
    retained=save_candidate(bpy,request)
    return {**response,"style_set":after,"event":event,"author_before_sha256":author,"author_after_sha256":updated,
            "state_sha256":fingerprint(after),"scene_sha256":scene_sha,"candidate_sha256":digest(request["stage"]),
            "retained_orphans":retained,"protected":{"status":"passed","line_groups_and_non_target_slots":True}}
