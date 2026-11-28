"""Native static Toon line template export, explicit rebinding, and application."""

import hashlib
import json
import os
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-line-template"
CAPTURE = "capture-line-template"
APPLY = "apply-line-template"
RESET = "reset-line-template"
OPERATIONS = (READ, CAPTURE, APPLY, RESET)
KINDS = {"objects", "materials", "collections"}


def template_ref(value):
    require(isinstance(value,dict) and value.keys()=={"file","sha256"},"INVALID_REQUEST",
            "template requires an absolute file and its SHA-256")
    path=value["file"]
    require(isinstance(path,str) and Path(path).is_absolute() and Path(path).suffix.lower()==".json"
            and isinstance(value["sha256"],str) and contract.SHA256.fullmatch(value["sha256"]),
            "INVALID_REQUEST","Expected a SHA-pinned absolute native template JSON")
    return dict(value)


def role_bindings(value):
    require(isinstance(value,dict) and value.keys()==KINDS,"INVALID_REQUEST",
            "bindings requires objects, materials and collections role maps")
    for kind,mapping in value.items():
        require(isinstance(mapping,dict) and len(mapping)<=4096 and all(contract.name(role,128)
            and contract.name(target,1024) for role,target in mapping.items()),"INVALID_REQUEST","Invalid "+kind+" role map")
    return value


def source_bindings(value):
    require(isinstance(value,dict) and len(value)<=256,"INVALID_REQUEST","ink_bindings must map policy IDs to sources")
    for identifier,data in value.items():
        require(contract.name(identifier,128) and isinstance(data,dict),"INVALID_REQUEST","Invalid ink binding")
        if data.keys()=={"node","direction","socket"}:
            require(contract.name(data["node"],1024) and contract.name(data["socket"],1024)
                    and data["direction"] in ("INPUT","OUTPUT"),"INVALID_REQUEST","Invalid constant ink node binding")
        else:
            require(data.keys()=={"image","uv_name","uv_transform"} and contract.name(data["image"],1024)
                    and contract.name(data["uv_name"],1024),"INVALID_REQUEST","Invalid UV image ink binding")
            coefficients=data["uv_transform"]
            require(isinstance(coefficients,list) and len(coefficients)==6 and all(type(entry) in (int,float)
                    and -65536<=entry<=65536 for entry in coefficients),"INVALID_REQUEST","Expected six UV affine coefficients")
    return value


def normalize_request(document,editions):
    operation=document["operation"]
    extra={"name","template","bindings","ink_bindings","mode","conflict"}
    payload={key:value for key,value in document.items() if key not in extra}
    seen=extra & document.keys()
    require(not {"scope","frames","elements","patch"} & payload.keys(),"INVALID_REQUEST","Template operations use explicit roles")
    if operation==CAPTURE:
        require(seen=={"name"} and contract.name(document["name"],128),"INVALID_REQUEST","Capture requires a template name")
    elif operation==RESET:
        require(not seen,"INVALID_REQUEST","Reset needs only the saved scene and author identity")
    else:
        require("name" not in seen,"INVALID_REQUEST","The template name is stored in its native JSON")
        require(not seen or "template" in seen,"INVALID_REQUEST","Template bindings require a native input file")
        if operation==APPLY:
            require({"template","bindings","mode","conflict"} <= seen,"INVALID_REQUEST",
                    "Application requires a native template, exact bindings, mode and conflict policy")
        elif "bindings" not in seen:
            require(seen <= {"template"},"INVALID_REQUEST","Import inspection without bindings accepts only template")
        else:
            require({"mode","conflict"} <= seen,"INVALID_REQUEST","Bound preflight needs explicit mode and conflict policy")
    if operation in (APPLY,RESET):
        payload["operation"]="render-camera"
    else:
        payload.update(operation="inspect",scope={"groups":["line-template"]})
    result=contract.normalize_request(payload,editions)
    result.pop("scope",None)
    result["operation"]=operation
    if "name" in seen:
        result["name"]=document["name"]
    if "template" in seen:
        result["template"]=template_ref(document["template"])
    if "bindings" in seen:
        mode,conflict=document["mode"],document["conflict"]
        require(mode in ("APPEND","REPLACE") and conflict in ("ERROR","RENAME"),"INVALID_REQUEST",
                "Choose APPEND/REPLACE and ERROR/RENAME")
        result.update(bindings=role_bindings(document["bindings"]),mode=mode,conflict=conflict)
        if "ink_bindings" in seen:
            result["ink_bindings"]=source_bindings(document["ink_bindings"])
    return result


def guard_inputs(request,guards):
    if "template" in request:
        data=request["template"]
        data["file"]=str(guard_file(data["file"],guards,data["sha256"]))


def stable_records(bpy,context,ui):
    current=ui.settings(context)
    archived={text.name:hashlib.sha256(text.as_string().encode("utf8")).hexdigest()
              for text in bpy.data.texts if text.get("toon_line_template_record") or text.get("toon_line_template_archive")}
    return {"template":json.loads(current.document) if current.document else None,
            "applied":ui.inspect_current(context) if current.receipt else None,"archives":archived}


def template_header(document):
    keys=("schema","name","template_id","revision","roles","ink_sources")
    return {key:document[key] for key in keys if key in document}


def native_control_order(document):
    from toon_workflow.toon_viewport_preview import style

    portable=document["style"]
    plan=style.compile_preset(portable["document"],portable["recipe"])
    controls=portable["scene_controls"]
    by_id={row["name"]:row for row in controls["groups"]}
    order=[group["id"] for group in sorted(plan["groups"],key=lambda row:row["priority"])]
    # set_preset regenerates static RNA rows by the compiled group priority.
    # Keep all values by identity while matching the native readback order.
    if set(order)==by_id.keys():
        controls["groups"]=[by_id[gid] for gid in order]
    return document


def install_roles(bpy,context,ui,core,document,request):
    current=ui.settings(context)
    targets=request["bindings"]
    current.mode=request["mode"]
    current.conflict=request["conflict"]
    pools={"objects":context.scene.objects,"materials":bpy.data.materials,"collections":bpy.data.collections}
    for kind in KINDS:
        roles={row["role"] for row in document["roles"][kind]}
        require(roles==targets[kind].keys(),"CONFLICT","Explicit "+kind+" roles must match the imported template")
    for row in current.bindings:
        name=targets[row.kind][row.role]
        value=pools[row.kind].get(name)
        require(value is not None and value.library is None and value.override_library is None,
                "NOT_FOUND","No local target for "+row.kind+" role "+row.role+": "+name)
        ui._bind_value(row,value)
    if document["schema"]!=core.SCHEMA_V2:
        require("ink_bindings" not in request,"INVALID_REQUEST","v1 does not accept ink source bindings")
        return
    replacements=request.get("ink_bindings",{})
    required={row.policy_id for row in current.ink_bindings if row.kind!="AUTHOR_RGB"}
    require(required==replacements.keys(),"CONFLICT","Explicit ink bindings must cover every node or image policy")
    for index,row in enumerate(current.ink_bindings):
        if row.kind=="AUTHOR_RGB":
            continue
        config=replacements[row.policy_id]
        if row.kind=="NODE_RGBA":
            require(config.keys()=={"node","direction","socket"},"INVALID_REQUEST","Expected node ink binding")
            row.node,row.direction,row.socket=(config[key] for key in ("node","direction","socket"))
        else:
            require(config.keys()=={"image","uv_name","uv_transform"},"INVALID_REQUEST","Expected UV image ink binding")
            row.image_name,row.uv_name,row.uv_transform=(config[key] for key in ("image","uv_name","uv_transform"))
        ui.bind_ink_current(context,index)


def load_template(bpy,context,ui,core,request):
    descriptor=request["template"]
    require(digest(descriptor["file"])==descriptor["sha256"],"CONFLICT","Template bytes changed")
    current=ui.settings(context)
    if current.document:
        ui.new_current(context)
    current.path=descriptor["file"]
    ui.import_current(context)
    document=native_control_order(json.loads(current.document))
    current.document=core._json(document)
    if "bindings" in request:
        install_roles(bpy,context,ui,core,document,request)
    return document


def work(bpy,context,request,job,author,*,save_candidate,author_sha,dependencies):
    from toon_workflow.toon_workflow_addon import joint_workflow, line_template as core, line_template_ui as ui

    scene=context.scene
    operation=request["operation"]
    response={"operation":operation,"product":request["product"],"context":request["context"],
              "source":{"file":request["file"],"sha256":request["expected_sha256"],"unchanged":True},
              "runtime":{"blender":bpy.app.version_string,"build":bpy.app.build_hash.decode(),"pid":os.getpid()}}
    protection=fingerprint([joint_workflow.non_author_signature(scene),dependencies()])
    try:
        if operation==CAPTURE:
            document=native_control_order(core.capture(scene,request["name"]))
            filename=job/"line-template.json"
            core.export_document(document,filename)
            require(core.load_document(filename)==document,"VALIDATION_FAILED","Exported native template could not reopen")
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Source changed during capture")
            return {**response,"template":template_header(document),"template_file":{"file":str(filename),"sha256":digest(filename)},
                    "author_sha256":author}

        if operation==READ:
            records=stable_records(bpy,context,ui)
            state_sha=fingerprint(records)
            verify=request.get("verify_state_sha256")
            if verify is not None:
                require(state_sha==verify and protection==request["verify_scene_sha256"],"VALIDATION_FAILED",
                        "Cold reopen changed native template or protected state")
            if "template" in request:
                document=load_template(bpy,context,ui,core,request)
                response["imported"]=template_header(document)
                response["template_file"]=request["template"]
                if "bindings" in request:
                    response["preflight"]=ui.inspect_current(context)
            require(digest(request["file"])==request["expected_sha256"],"CONFLICT","Source changed during inspection")
            return {**response,"record":records,"author_sha256":author,"state_sha256":state_sha,"scene_sha256":protection}

        require(scene.library is None and scene.override_library is None and scene.is_editable,"UNSUPPORTED",
                "Template mutation requires a local editable scene")
        if operation==APPLY:
            document=load_template(bpy,context,ui,core,request)
            result=ui.apply_current(context)
            require(result["template_id"]==document["template_id"],"VALIDATION_FAILED","Applied template identity differs")
            response["template"]=template_header(document)
            response["template_file"]=request["template"]
        else:
            require(ui.settings(context).document,"CONFLICT","The scene has no template record to archive")
            ui.new_current(context)
            require(author_sha(scene)==author,"VALIDATION_FAILED","Reset changed the native working style")
        state=stable_records(bpy,context,ui)
    except (ValueError,KeyError,TypeError,OverflowError) as error:
        raise Failure("CONFLICT",str(error)) from error
    require(fingerprint([joint_workflow.non_author_signature(scene),dependencies()])==protection,"VALIDATION_FAILED",
            "Template changed geometry, material slot binding or external file paths")
    updated=author_sha(scene)
    retained=save_candidate(bpy,request)
    return {**response,"record":state,"author_before_sha256":author,"author_after_sha256":updated,
            "state_sha256":fingerprint(state),"scene_sha256":protection,"candidate_sha256":digest(request["stage"]),
            "retained_orphans":retained,"protected":{"status":"passed","source_scene_geometry_and_bindings":True}}
