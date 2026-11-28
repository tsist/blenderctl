"""Native Style Family packages, verified application and saved recovery."""

import hashlib
import json
import os
from pathlib import Path
import shutil

from material_draft_handoff import fingerprint, guard_file, open_saved_context, read_document, require
from protocol import Failure, digest
from toon_cel_recipe import rebind, stored_slots, validate_slot
from toon_viewport_line_template import install_roles, role_bindings, source_bindings, template_ref
import toon_viewport_lines_contract as contract


READ="inspect-style-family"
EDIT="edit-style-family"
EXPORT="export-style-family"
OPERATIONS=(READ,EDIT,EXPORT)
SCHEMAS=("toon-style-family.v1","toon-style-family.v2")
ACTIONS={
    "NEW":({"name","line_mode"},set()),
    "NEW_V2":({"name","line_mode","family_kind"},{"background_roles"}),
    "SYNC_SUN":({"sun"},set()),"IMPORT":({"package"},set()),
    "APPLY":(set(),{"verify_rollback"}),"ARCHIVE":(set(),set()),"RESTORE":(set(),{"application_receipt"}),
}


def normalize_request(document,editions):
    operation=document["operation"]
    extra={"family_action","expected_family","preflight","rebind_slots","line_bindings"}
    payload={key:value for key,value in document.items() if key not in extra}
    require(not {"scope","frames","elements","patch"} & payload.keys(),"INVALID_REQUEST","Family uses explicit role bindings")
    if operation==EDIT:
        require("preflight" not in document,"INVALID_REQUEST","Use read operation for preflight")
        payload["operation"]="render-camera"
    else:
        require("family_action" not in document,"INVALID_REQUEST","Read/export accepts no family edits")
        require(operation==READ or not ({"preflight","rebind_slots","line_bindings"} & document.keys()),
                "INVALID_REQUEST","Export uses the saved family document")
        payload.update(operation="inspect",scope={"groups":["style-family"]})
    result=contract.normalize_request(payload,editions)
    result.pop("scope",None)
    result["operation"]=operation
    if "expected_family" in document:
        version=document["expected_family"]
        require(isinstance(version,dict) and version.keys()=={"id","schema"}
                and contract.name(version["id"],128) and version["schema"] in SCHEMAS,"INVALID_REQUEST",
                "expected_family requires exact id and supported schema")
        result["expected_family"]=version
    if "preflight" in document:
        require(document["preflight"] is True,"INVALID_REQUEST","preflight, when supplied, must be true")
        result["preflight"]=True
    if "rebind_slots" in document:
        slots=document["rebind_slots"]
        require(isinstance(slots,list) and 1<=len(slots)<=4096,"INVALID_REQUEST","Expected explicit saved slot bindings")
        for item in slots:
            validate_slot(item)
        result["rebind_slots"]=slots
    if "line_bindings" in document:
        line=document["line_bindings"]
        require(isinstance(line,dict) and {"bindings"}<=line.keys()<={"bindings","ink_bindings"},"INVALID_REQUEST",
                "line_bindings needs exact style template roles and applicable ink bindings")
        result["line_bindings"]={"bindings":role_bindings(line["bindings"])}
        if "ink_bindings" in line:
            result["line_bindings"]["ink_bindings"]=source_bindings(line["ink_bindings"])
    if operation==EDIT:
        step=document.get("family_action")
        require(isinstance(step,dict) and isinstance(step.get("action"),str) and step["action"] in ACTIONS,
                "INVALID_REQUEST","Unknown Style Family action")
        cmd=step["action"]
        required,optional=ACTIONS[cmd]
        require(required<=step.keys()-{"action"}<=required|optional,"INVALID_REQUEST","Unexpected family action fields")
        require(not ({"rebind_slots","line_bindings"} & result.keys()) or cmd in ("NEW","NEW_V2","APPLY"),
                "INVALID_REQUEST","Explicit bindings are used when creating or applying a family")
        for key in ("name","sun"):
            if key in step:
                require(contract.name(step[key],128),"INVALID_REQUEST","Invalid family "+key)
        if "line_mode" in step:
            require(step["line_mode"] in ("PRESERVE","TEMPLATE"),"INVALID_REQUEST","Unknown family line mode")
        if "family_kind" in step:
            require(step["family_kind"] in ("CEL","GAME","MANGA"),"INVALID_REQUEST","Unknown family kind")
            require(("background_roles" in step)==(step["family_kind"]=="MANGA"),"INVALID_REQUEST",
                    "MANGA requires an explicit background role list")
        if "background_roles" in step:
            roles=step["background_roles"]
            require(isinstance(roles,list) and all(contract.name(role,128) and "," not in role and role.strip()==role for role in roles)
                    and len(roles)==len(set(roles)),"INVALID_REQUEST","Invalid explicit background role list")
        step=dict(step)
        if "package" in step:
            step["package"]=template_ref(step["package"])
        if "application_receipt" in step:
            step["application_receipt"]=file_ref(step["application_receipt"],".json")
        if "verify_rollback" in step:
            require(step["verify_rollback"] is True,"INVALID_REQUEST","verify_rollback, when supplied, must be true")
        result["family_action"]=step
    elif ({"rebind_slots","line_bindings"} & result.keys()):
        require(result.get("preflight"),"INVALID_REQUEST","Read-side bindings require preflight=true")
    return result


def guard_inputs(request,guards):
    step=request.get("family_action",{})
    if "package" in step:
        package=step["package"]
        package["file"]=str(guard_file(package["file"],guards,package["sha256"]))
    if step.get("action")=="RESTORE":
        request["family_restore"]=guard_recovery(request,step.get("application_receipt"),guards)


def file_ref(value,suffix):
    require(isinstance(value,dict) and value.keys()=={"file","sha256"} and isinstance(value["file"],str)
            and Path(value["file"]).is_absolute() and Path(value["file"]).suffix.lower()==suffix
            and isinstance(value["sha256"],str) and contract.SHA256.fullmatch(value["sha256"]),
            "INVALID_REQUEST","Expected an absolute file and SHA-256 for the Style Family "+suffix+" reference")
    return dict(value)


def guard_recovery(request,reference,guards):
    require(reference is not None,"UNSUPPORTED",
            "RESTORE requires application_receipt {file,sha256} from a successful public Style Family APPLY")
    receipt_path=guard_file(reference["file"],guards,reference["sha256"])
    receipt=read_document(receipt_path)
    require(isinstance(receipt,dict) and receipt.get("command")=="toon-workflow.viewport-lines"
            and receipt.get("ok") is True and isinstance(receipt.get("data"),dict),
            "INVALID_REQUEST","Expected a successful public Style Family APPLY receipt")
    data=receipt["data"]
    event=data.get("event")
    verification=data.get("verification")
    require(data.get("operation")==EDIT and isinstance(event,dict) and "applied" in event
            and isinstance(verification,dict) and verification.get("status")=="passed",
            "INVALID_REQUEST","Receipt does not describe a verified Style Family APPLY")
    origin=file_ref(event.get("recovery_source"),".blend")
    candidate=file_ref(data.get("candidate"),".blend")
    source=data.get("source")
    require(isinstance(source,dict) and source.get("file")==origin["file"] and source.get("sha256")==origin["sha256"]
            and source.get("unchanged") is True and Path(candidate["file"])!=Path(origin["file"]),
            "INVALID_REQUEST","The receipt has no distinct verified pre-apply source")
    product=data.get("product")
    family=data.get("family")
    require(isinstance(product,dict) and isinstance(family,dict) and isinstance(family.get("document"),dict)
            and isinstance(family.get("applied"),dict) and fingerprint(family)==data.get("state_sha256"),
            "INVALID_REQUEST","Receipt is missing its applied family or frozen state digest")
    doc=family["document"]
    version={"id":doc.get("family_id"),"schema":doc.get("schema")}
    require(doc.get("schema") in SCHEMAS and contract.name(version["id"],128),"INVALID_REQUEST",
            "Receipt is missing the saved native family identity")
    for field in ("state_sha256","scene_sha256","author_before_sha256","author_after_sha256"):
        require(isinstance(data.get(field),str) and contract.SHA256.fullmatch(data[field]),
                "INVALID_REQUEST","Receipt is missing "+field)
    recovery_hint=" Recorded pre-apply source: "+origin["file"]+"."
    require(data.get("context")==request["context"] and product.get("edition")==request["edition"]
            and request.get("expected_family",version)==version,"CONFLICT","Apply receipt belongs to a different context or family."+recovery_hint)
    require(Path(candidate["file"])==Path(request["file"]) and candidate["sha256"]==request["expected_sha256"]
            and request.get("expected_author_sha256")==data["author_after_sha256"] and
            candidate["sha256"]==data.get("candidate_sha256"),"CONFLICT",
            "Current candidate does not match the applied receipt; later saved edits cannot be restored as a whole."+recovery_hint)
    try:
        request["file"]=str(guard_file(request["file"],guards,candidate["sha256"]))
    except (Failure,OSError) as error:
        raise Failure("CONFLICT","Applied candidate changed or is unavailable; no recovery file was published."+recovery_hint) from error
    try:
        origin["file"]=str(guard_file(origin["file"],guards,origin["sha256"]))
    except (Failure,OSError) as error:
        raise Failure("CONFLICT","Recorded pre-apply source is missing or changed; its original bytes are required."+recovery_hint) from error
    return {"receipt":{"file":str(receipt_path),"sha256":reference["sha256"]},"source":origin,"family":version,
            "product":product,"applied_state_sha256":data["state_sha256"],"applied_scene_sha256":data["scene_sha256"],
            "before_author_sha256":data["author_before_sha256"]}


def family_state(bpy,context,ui,cel):
    settings=ui.settings(context)
    doc=ui.document(context) if settings.family_id else None
    archives={text.name:hashlib.sha256(text.as_string().encode("utf8")).hexdigest() for text in bpy.data.texts
              if text.get("toon_style_family_archive") or text.get("toon_cel_author_archive")}
    return {"document":doc,"slots":stored_slots(cel._settings(context)),"archives":archives,
            "sun":settings.sun.name if settings.sun else None,
            "applied":ui.inspect_current(context) if settings.receipt else None}


def check_version(state,request):
    if "expected_family" in request:
        doc=state["document"]
        actual={"id":doc["family_id"],"schema":doc["schema"]} if doc else None
        require(actual==request["expected_family"],"CONFLICT","Saved family id or schema differs")


def configure_bindings(bpy,context,cel,request,line_mode):
    rebind(cel,context,request.get("rebind_slots"))
    supplied=request.get("line_bindings")
    if supplied is None:
        return
    require(line_mode=="TEMPLATE","INVALID_REQUEST","Line template bindings require TEMPLATE mode")
    from toon_workflow.toon_workflow_addon import line_template as core, line_template_ui as ui

    raw=ui.settings(context).document
    require(raw,"NOT_FOUND","The family lacks its embedded line template")
    install_roles(bpy,context,ui,core,json.loads(raw),{**supplied,"mode":"REPLACE","conflict":"ERROR"})


def manifests(context,cel,doc,read_bindings):
    selected=set()
    if doc is not None:
        for row in stored_slots(cel._settings(context)):
            role=doc["recipe"]["roles"].get(row["role"],{})
            if role.get("action")=="SURFACE":
                selected.add((row["object"],row["slot_index"]))
    rows=read_bindings()
    for row in rows:
        for index,slot in enumerate(row["materials"]):
            if (row["object"],index) in selected:
                slot[1]=None
    return rows


def transaction(bpy,context,ui,cel,request,read_bindings,author_sha):
    step=request["family_action"]
    cmd=step["action"]
    settings=ui.settings(context)
    if cmd=="IMPORT":
        package=step["package"]
        require(digest(package["file"])==package["sha256"],"CONFLICT","Family JSON changed")
        settings.path=package["file"]
        return ui.import_current(context)
    if cmd=="ARCHIVE":
        return ui.archive_current(context)
    if cmd=="SYNC_SUN":
        sun=context.scene.objects.get(step["sun"])
        require(sun is not None and sun.type=="LIGHT" and sun.data.type=="SUN" and sun.library is None,
                "NOT_FOUND","Unknown local Sun: "+step["sun"])
        settings.sun=sun
        return ui.sync_sun(context)
    if cmd in ("NEW","NEW_V2"):
        settings.name=step["name"]
        settings.line_mode=step["line_mode"]
        configure_bindings(bpy,context,cel,request,settings.line_mode)
        if cmd=="NEW":
            return ui.new_current(context)
        settings.family_kind=step["family_kind"]
        if "background_roles" in step:
            settings.background_roles=",".join(step["background_roles"]) or "[]"
        return ui.new_current_v2(context)

    require(not settings.receipt,"CONFLICT","This family is already applied; inspect its saved result")
    configure_bindings(bpy,context,cel,request,settings.line_mode)
    before={"author":author_sha(context.scene),"slots":read_bindings(),"family":ui.document(context)}
    event={"applied":ui.apply_current(context),"recovery_source":{"file":request["file"],"sha256":request["expected_sha256"]}}
    if step.get("verify_rollback"):
        first=ui.inspect_current(context)
        restored=ui.restore_current(context)
        after={"author":author_sha(context.scene),"slots":read_bindings(),"family":ui.document(context)}
        require(after==before and not settings.receipt and not cel._settings(context).receipt,"VALIDATION_FAILED",
                "Native same-process rollback did not return to the pre-apply family")
        event["verified_rollback"]={"kind":"same_blender_process","restored":restored,"first_inspection":first}
        event["applied"]=ui.apply_current(context)
    return event


def restore(bpy,request,ui,cel,before,author,scene_sha,response,author_sha,dependencies,read_bindings):
    recovery=request["family_restore"]
    source=recovery["source"]
    require(request["product"]==recovery["product"] and before["applied"] is not None
            and fingerprint(before)==recovery["applied_state_sha256"] and scene_sha==recovery["applied_scene_sha256"],
            "CONFLICT","Current saved application no longer matches its verified receipt; pre-apply source: "+source["file"])
    require(digest(source["file"])==source["sha256"],"CONFLICT","Pre-apply source SHA changed")
    context=open_saved_context(bpy,{**request,"file":source["file"]})
    recovered=family_state(bpy,context,ui,cel)
    check_version(recovered,{"expected_family":recovery["family"]})
    recovered_author=author_sha(context.scene)
    require(recovered["applied"] is None and recovered_author==recovery["before_author_sha256"],"CONFLICT",
            "Pre-apply source does not match the recorded native family before application")
    recovered_scene=fingerprint([read_bindings(),dependencies()])
    stage=Path(request["stage"])
    origin=Path(source["file"])
    relative=any(path.startswith("//") for paths in bpy.data.file_path_map().values() for path in paths)
    if relative and stage.parent!=origin.parent:
        bpy.context.preferences.filepaths.save_version=0
        require(bpy.ops.wm.save_as_mainfile(filepath=str(stage),copy=True,relative_remap=True,
                                           check_existing=False)=={"FINISHED"},"IO_ERROR","Could not write recovery copy")
        mode="remapped_relative_paths"
    else:
        shutil.copyfile(origin,stage)
        require(digest(stage)==source["sha256"],"VALIDATION_FAILED","Exact recovery copy differs from the original")
        mode="exact_snapshot"
    require(digest(request["file"])==request["expected_sha256"] and digest(origin)==source["sha256"],
            "CONFLICT","A guarded input changed before recovery completed")
    event={"status":"restored","mode":mode,"recovery_source":source,"application_receipt":recovery["receipt"]}
    return {**response,"family":recovered,"event":event,"author_before_sha256":author,
            "author_after_sha256":recovered_author,"state_sha256":fingerprint(recovered),"scene_sha256":recovered_scene,
            "candidate_sha256":digest(stage),"retained_orphans":[],
            "protected":{"status":"passed","preserved_and_non_target_slots":True,
                         "applied_candidate_unchanged":True,"baseline_snapshot_unchanged":True}}


def work(bpy,context,request,job,author,*,save_candidate,author_sha,dependencies,read_bindings):
    from toon_workflow.toon_workflow_addon import cel_ui as cel, style_family_ui as ui

    scene=context.scene
    operation=request["operation"]
    response={"operation":operation,"product":request["product"],"context":request["context"],
        "source":{"file":request["file"],"sha256":request["expected_sha256"],"unchanged":True},
        "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    try:
        before=family_state(bpy,context,ui,cel)
        check_version(before,request)
        scene_sha=fingerprint([read_bindings(),dependencies()])
        if operation==READ:
            state_sha=fingerprint(before)
            if "verify_state_sha256" in request:
                require(state_sha==request["verify_state_sha256"] and scene_sha==request["verify_scene_sha256"],"VALIDATION_FAILED",
                        "Cold reopen changed the native family or its scene references")
            if request.get("preflight"):
                configure_bindings(bpy,context,cel,request,ui.settings(context).line_mode)
                response["preflight"]=ui.inspect_current(context)
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Family source changed during inspection")
            return {**response,"family":before,"author_sha256":author,"state_sha256":state_sha,"scene_sha256":scene_sha}
        if operation==EXPORT:
            filename=job/"style-family.json"
            ui.settings(context).path=str(filename)
            ui.export_current(context)
            require(json.loads(filename.read_text(encoding="utf8"))==before["document"],"VALIDATION_FAILED",
                    "Native family export differs from the saved document")
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Family source changed during export")
            return {**response,"family":before,"package":{"file":str(filename),"sha256":digest(filename)},"author_sha256":author}

        if request["family_action"]["action"]=="RESTORE":
            return restore(bpy,request,ui,cel,before,author,scene_sha,response,author_sha,dependencies,read_bindings)
        require(scene.library is None and scene.override_library is None and scene.is_editable,"UNSUPPORTED","Family editing needs a local scene")
        affected=before["document"] if request["family_action"]["action"]=="APPLY" else None
        protected=manifests(context,cel,affected,read_bindings)
        event=transaction(bpy,context,ui,cel,request,read_bindings,author_sha)
        after=family_state(bpy,context,ui,cel)
        require(after!=before,"CONFLICT","Family action did not change native saved state")
        require(manifests(context,cel,affected,read_bindings)==protected,"VALIDATION_FAILED",
                "Family edit changed a preserved or non-target material slot")
    except (ValueError,TypeError,KeyError) as error:
        raise Failure("CONFLICT",str(error)) from error
    updated=author_sha(scene)
    scene_sha=fingerprint([read_bindings(),dependencies()])
    retained=save_candidate(bpy,request)
    # Do not serialize runtime tokens. The saved source is the cross-process recovery authority.
    return {**response,"family":after,"event":event,"author_before_sha256":author,"author_after_sha256":updated,
            "state_sha256":fingerprint(after),"scene_sha256":scene_sha,"candidate_sha256":digest(request["stage"]),
            "retained_orphans":retained,"protected":{"status":"passed","preserved_and_non_target_slots":True}}
