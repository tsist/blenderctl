# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned, strict request/result protocol. Standard library only."""
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
import time
import uuid

VERSION = "0.55.1"
SCHEMA = "1.0"
ROOT = Path(__file__).resolve().parents[2]
_PORTABLE_BLENDER = ROOT / "runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe"
DEFAULT_BLENDER = Path(os.environ.get("BLENDER_PATH") or
                       (str(_PORTABLE_BLENDER) if _PORTABLE_BLENDER.is_file() else shutil.which("blender")) or
                       str(_PORTABLE_BLENDER))
DEFAULT_JOBS = Path(os.environ.get("BLENDERCTL_JOBS_DIR") or ROOT / "runtime/blenderctl/jobs")
DEFAULT_TRANSACTIONS = Path(os.environ.get("BLENDERCTL_TRANSACTIONS_DIR") or ROOT / "runtime/blenderctl/transactions")
CODES = {"INVALID_REQUEST": 2, "NOT_FOUND": 3, "CONFLICT": 4,
         "WORKER_FAILED": 5, "TIMEOUT": 6, "CANCELLED": 7,
         "VALIDATION_FAILED": 8, "IO_ERROR": 9, "UNSUPPORTED": 10, "RESOURCE_LIMIT":11}
HOST_COMMANDS = ("pipeline.plan", "pipeline.run", "pipeline.resume", "asset.plan", "project.plan_copy", "project.plan_batch", "project.plan_files", "transaction.apply", "transaction.recover", "transaction.rollback", "transaction.status")
COMMANDS = ("doctor", "capabilities", "inspect", "query", "diff", "dependency.audit", "validate", "identity.prepare", "asset.prepare", "asset.index", "scene.prepare", "scene.inspect", "model.prepare", "model.inspect", "node.prepare", "node.inspect", "material.preview", "texture.bake", "rig.prepare", "rig.inspect", "rig.package", "animation.sample", "simulation.prepare", "simulation.inspect", "simulation.bake", "render.devices", "render.run", "media.prepare", "media.export", "exchange.formats", "exchange.analyze", "exchange.export", "exchange.import", "exchange.convert", "extension.inspect", "extension.prepare", "extension.run", "probe", *HOST_COMMANDS)


HOST_COMMANDS = (*HOST_COMMANDS, 'asset.preview.batch')
COMMANDS = (*COMMANDS, 'asset.preview.prepare', 'asset.preview.batch', 'dependency.plan-relink', 'dependency.prepare', 'project.link.prepare', 'project.override.prepare', 'project.override.resync')
COMMANDS = (*COMMANDS, 'tracking.prepare', 'tracking.inspect', 'tracking.solve')
COMMANDS = (*COMMANDS, 'project.verify', 'project.prepare_copy')
OPERATIONS=('resource.init','resource.status','cache.inspect','cache.plan_clean','project.package','doctor.verify_install')
REVIEWS=('extension.review','extension.approve')
HOST_COMMANDS=(*HOST_COMMANDS,*REVIEWS)
COMMANDS=(*COMMANDS,*REVIEWS,'sculpt.review','sculpt.replay')
HOST_COMMANDS=(*HOST_COMMANDS,*OPERATIONS)
COMMANDS=(*COMMANDS,*OPERATIONS)
COMMANDS=(*COMMANDS,'material.run','material.template-save','material.batch','material.study')

class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def read_json(path):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Failure("INVALID_REQUEST", f"Duplicate JSON key: {key}")
            out[key] = value
        return out
    try:
        if os.name == "nt":
            from filesystem import read_shared_bytes
            text = read_shared_bytes(path).decode("utf-8-sig")
        else:
            text = Path(path).read_text(encoding="utf-8-sig")
        return json.loads(text, object_pairs_hook=pairs)
    except FileNotFoundError as exc:
        raise Failure("NOT_FOUND", f"JSON file not found: {path}") from exc
    except (ValueError, UnicodeError) as exc:
        raise Failure("INVALID_REQUEST", f"Invalid UTF-8 JSON: {exc}") from exc


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Windows can briefly retain a delete-pending destination while shared
    # readers finish the old JSON generation. Retry only this metadata operation,
    # never a transaction publish or a failed worker. Keep a hard time bound.
    deadline = time.monotonic() + .25
    while True:
        try:
            os.replace(temp, path)
            break
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(.005)


def digest(path, checkpoint=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if checkpoint:
                checkpoint()
            h.update(chunk)
    return h.hexdigest()


def validate(request):
    if not isinstance(request, dict):
        raise Failure("INVALID_REQUEST", "Request must be an object")
    extra = set(request) - {"schema_version", "command", "params"}
    if extra or request.get("schema_version") != SCHEMA:
        raise Failure("INVALID_REQUEST", f"Expected schema_version {SCHEMA}; unknown fields: {sorted(extra)}")
    command = request.get("command")
    if not isinstance(command, str) or command not in COMMANDS:
        raise Failure("INVALID_REQUEST", f"Unknown command: {command}")
    params = request.get("params", {})
    if command=='material.run':
        from material_workflow_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params)}
    if command=='material.batch':
        from material_batch_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params)}
    if command=='material.study':
        from material_study_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params)}
    if command=='material.template-save':
        from material_workflow_contract import normalize_template_save_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_template_save_params(params)}
    if command=='sculpt.replay':
        from sculpt_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params)}
    if command in (*REVIEWS,'sculpt.review'):
        from review_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params,command)}
    if command=='extension.run' and 'review_receipt' in params:
        from exchange_contract import descriptor
        descriptor(params['review_receipt'])
    if command in OPERATIONS:
        from operations_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params,command)}
    if command in ('project.verify','project.prepare_copy'):
        from project_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(request.get('params',{}),command)}
    if command in ('tracking.prepare','tracking.inspect','tracking.solve'):
        from tracking_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params,command)}
    allowed = {"doctor": set(), "capabilities": set(), "inspect": {"file","expected_sha256"}, "probe": {"case"},
               "pipeline.plan":{'manifest'}, "pipeline.run":{'manifest','reuse_job'}, "pipeline.resume":{'job_id'},
               "query": {"file", "selector", "expected_sha256", "profile"},
               "diff": {"file", "other", "expected_sha256", "other_sha256", "profile"},
               "dependency.audit": {"file", "expected_sha256", "profile"}, "validate": {"file", "expected_sha256", "profile"},
               "dependency.plan-relink": {"file", "expected_sha256", "manifest"}, "dependency.prepare": {"file", "expected_sha256", "plan"},
               "identity.prepare": {"file", "expected_sha256", "selectors"},
               "asset.prepare": {"file", "expected_sha256", "manifest"}, "asset.index": {"file", "expected_sha256", "manifest"},
               "asset.plan": {"manifest"},
               "asset.preview.prepare": {'file','expected_sha256','manifest','resources','driver_profile','simulation_receipt','reuse_receipt'},
               "asset.preview.batch": {'manifest','reuse_job'},
               "scene.prepare": {"file","expected_sha256","manifest"}, "scene.inspect": {"file","expected_sha256","context"},
               "model.prepare": {"file","expected_sha256","manifest"}, "model.inspect": {"file","expected_sha256","context"},
               "node.prepare": {"file","expected_sha256","manifest","resources"}, "node.inspect": {"file","expected_sha256","context"},
               "rig.prepare": {"file","expected_sha256","manifest","driver_profile"}, "rig.inspect": {"file","expected_sha256","context","suggest_mapping"}, "animation.sample": {"file","expected_sha256","manifest","driver_profile"},
               "rig.package": {"file","expected_sha256","manifest","driver_profile","simulation_receipt"},
               "simulation.prepare": {"file","expected_sha256","manifest","driver_profile"}, "simulation.inspect": {"file","expected_sha256","context","hair_frames","driver_profile"}, "simulation.bake": {"file","expected_sha256","manifest","driver_profile","resources"},
               "material.preview": {"file","expected_sha256","material","resolution","samples","frame","resources"},
               "texture.bake": {"file","expected_sha256","object","scene","view_layer","uv_layer","type","resolution","samples","margin","frame","resources"},
               "exchange.formats":set(), "exchange.analyze":{"file","expected_sha256","manifest","resources","driver_profile","simulation_receipt"}, "exchange.export":{"file","expected_sha256","manifest","resources","driver_profile","simulation_receipt"}, "exchange.import":{"file","expected_sha256","manifest"}, "exchange.convert":{"manifest"}, "extension.inspect":set(), "extension.prepare":{"manifest"}, "extension.run":{"manifest","resources","review_receipt"},
               "render.devices": set(), "render.run": {"file","expected_sha256","manifest","resources","simulation_receipt","driver_profile"},
               "media.prepare": {"manifest"}, "media.export": {"file","expected_sha256","manifest","resources"},
               "project.plan_copy": {"file", "output"}, "project.plan_batch": {"items"}, "project.plan_files": {"items"},
               **{c: {'file', 'expected_sha256', 'manifest'} for c in ('project.link.prepare', 'project.override.prepare', 'project.override.resync')},
               **{c: {"transaction_id"} for c in HOST_COMMANDS if c.startswith("transaction.")}}[command]
    if not isinstance(params, dict) or set(params) - allowed:
        raise Failure("INVALID_REQUEST", f"Invalid params for {command}")
    params = dict(params)
    if command in ('project.link.prepare', 'project.override.prepare', 'project.override.resync'):
        from link_contract import normalize
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize(params, command)}
    if command in ('dependency.plan-relink','dependency.prepare'):
        from dependency_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params,command)}
    if 'driver_profile' in params:
        if command=='simulation.prepare' and (not params.get('manifest',{}).get('operations') or any(op.get('op') not in ('hair.surface','hair.shape','hair.dynamics','hair.density','hair.volume') for op in params['manifest']['operations'])):raise Failure('INVALID_REQUEST','Simulation prepare driver profile requires a pure hair manifest')
        if command=='simulation.inspect' and 'hair_frames' not in params:raise Failure('INVALID_REQUEST','Simulation inspect driver profile requires hair_frames')
        if command=='rig.prepare' and any(op.get('op') not in ('animation.retarget_pose','skin.transfer_weights','skin.auto_weights','skin.rebind') for op in params.get('manifest',{}).get('operations',[])):
            raise Failure('INVALID_REQUEST','Rig driver profile permits only explicit S08 retarget/weight transfer adapters')
        from driver_contract import normalize as normalize_driver_profile
        params['driver_profile']=normalize_driver_profile(params['driver_profile'])
        if not params.get('expected_sha256'):raise Failure('INVALID_REQUEST','Driver profile requires expected_sha256')
        if params['expected_sha256']!=params['driver_profile']['source_sha256']:
            raise Failure('CONFLICT','Driver profile requires matching expected_sha256')
    if command in ('asset.preview.prepare','asset.preview.batch'):
        from preview_contract import normalize_params,expand_batch
        if command=='asset.preview.prepare':params=normalize_params(params)
        else:
            expand_batch(params.get('manifest'))
            if 'reuse_job' in params:
                import re
                if not isinstance(params['reuse_job'],str) or not re.fullmatch('[0-9a-f]{32}',params['reuse_job']):raise Failure('INVALID_REQUEST','Prior batch requires a 32-character job ID')
        return {'schema_version':SCHEMA,'command':command,'params':params}
    if command.startswith('pipeline.'):
        from pipeline_contract import normalize_request
        return {'schema_version':SCHEMA,'command':command,'params':normalize_request(command,params)}
    if command in ('exchange.analyze','exchange.export','exchange.import','exchange.convert','extension.run','extension.prepare'):
        from exchange_contract import normalize,descriptor
        params['manifest']=normalize(params.get('manifest'),command.split('.')[1])
        if command in ('exchange.export','exchange.analyze'):
            adapter=command=='exchange.analyze' or params['manifest'].get('rig_adapter') or params['manifest'].get('geometry_adapter')
            if adapter and not params.get('driver_profile'):raise Failure('INVALID_REQUEST','Native evaluated exchange requires a source-bound driver profile')
            if 'simulation_receipt' in params:
                if not adapter:raise Failure('INVALID_REQUEST','Exchange simulation receipt requires an explicit evaluated adapter')
                descriptor(params['simulation_receipt'])
        if command in ('exchange.export','exchange.import','exchange.analyze'):
            if 'file' not in params or 'expected_sha256' not in params:raise Failure('INVALID_REQUEST','Exchange requires input file and SHA256')
            native_cache=command=='exchange.import' and params['manifest'].get('geometry_adapter') in ('EVALUATED_MESH_CACHE_V1','FBX_NATIVE_CLOCK_V1','GLB_NATIVE_CLOCK_V1')
            descriptor({k:params[k] for k in ('file','expected_sha256')},max_bytes=1024**3 if native_cache else 256*1024**2);params['file']=str(Path(params['file']).resolve())
            if command in ('exchange.export','exchange.analyze') and Path(params['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Export source must be .blend')
            if command=='exchange.import':
                from exchange_contract import SUFFIX
                allowed=(SUFFIX[params['manifest']['format']],) if params['manifest']['format']!='USD' else ('.usd','.usda','.usdc')
                if Path(params['file']).suffix.lower() not in allowed:raise Failure('INVALID_REQUEST','Import format does not match suffix')
    if command in ('render.run','media.prepare','media.export'):
        from render_contract import normalize_run,normalize_media,normalize_export
        params['manifest']={'render.run':normalize_run,'media.prepare':normalize_media,'media.export':normalize_export}[command](params.get('manifest'))
        if command!='media.prepare':
            if 'expected_sha256' not in params:raise Failure('INVALID_REQUEST','Output operations require source SHA256')
            params.update(validate({'schema_version':SCHEMA,'command':'query','params':{k:params[k] for k in ('file','expected_sha256') if k in params}})['params'])
        if 'simulation_receipt' in params:
            from node_contract import FILE,validate as validate_nodes
            from render_contract import file_input
            validate_nodes(params['simulation_receipt'],FILE);file_input(params['simulation_receipt'])
    if command in ('scene.inspect','model.inspect','node.inspect','rig.inspect','simulation.inspect') and 'context' in params:
        from scene_contract import CONTEXT,validate as validate_scene
        validate_scene(params['context'],CONTEXT)
    if command=='rig.inspect' and 'suggest_mapping' in params:
        from scene_contract import obj,NAME,validate as validate_scene
        validate_scene(params['suggest_mapping'],obj({'source':NAME,'target':NAME}))
    if command=='simulation.inspect' and 'hair_frames' in params:
        from hair_contract import FRAMES
        from scene_contract import validate as validate_scene
        validate_scene(params['hair_frames'],FRAMES)
    if command in ('scene.prepare','model.prepare','node.prepare','rig.prepare','simulation.prepare'):
        if command=='scene.prepare':from scene_contract import normalize
        elif command=='model.prepare':from model_contract import normalize
        elif command=='node.prepare':from node_contract import normalize
        elif command=='simulation.prepare':from simulation_contract import normalize
        else:from rig_contract import normalize
        params['manifest']=normalize(params.get('manifest'))
        if command=='simulation.prepare' and any(op['op'].startswith('hair.') for op in params['manifest']['operations']) and not params.get('file'):raise Failure('INVALID_REQUEST','Hair prepare requires an existing source file')
        if 'file' in params:
            if 'expected_sha256' not in params or 'initial_scene' in params['manifest']:
                raise Failure('INVALID_REQUEST','Existing scene edits require SHA256 and cannot specify initial_scene')
            params.update(validate({'schema_version':SCHEMA,'command':'query','params':{k:params[k] for k in ('file','expected_sha256')}})['params'])
        elif 'expected_sha256' in params:raise Failure('INVALID_REQUEST','SHA256 requires an input file')
    if command=='animation.sample':
        from rig_contract import SAMPLE,validate as validate_rig
        validate_rig(params.get('manifest'),SAMPLE)
    if command=='rig.package':
        from rig_package_contract import normalize
        from exchange_contract import descriptor
        params['manifest']=normalize(params.get('manifest'))
        if not {'file','expected_sha256','driver_profile'}<=set(params):raise Failure('INVALID_REQUEST','Native rig package requires file, SHA and driver profile')
        params.update(validate({'schema_version':SCHEMA,'command':'query','params':{k:params[k] for k in ('file','expected_sha256')}})['params'])
        if 'simulation_receipt' in params:descriptor(params['simulation_receipt'])
    if command=='simulation.bake':
        from simulation_contract import bake_contract
        params['manifest']=bake_contract(params.get('manifest'))
        adapter=params['manifest'].get('adapter')
        if adapter=='RIG_CLOTH_V1' and not params.get('driver_profile'):raise Failure('INVALID_REQUEST','RIG_CLOTH_V1 requires a driver profile')
        if 'driver_profile' in params and adapter not in ('RIG_CLOTH_V1','HAIR_CLOTH_V1'):raise Failure('INVALID_REQUEST','Driver profile requires RIG_CLOTH_V1 or HAIR_CLOTH_V1')
        if 'resources' in params and params['manifest'].get('adapter')!='GEOMETRY_ZONE_V1':raise Failure('INVALID_REQUEST','Simulation resources require GEOMETRY_ZONE_V1')
        if 'expected_sha256' not in params:raise Failure('INVALID_REQUEST','Simulation bake/reuse requires expected_sha256')
        params.update(validate({'schema_version':SCHEMA,'command':'query','params':{k:params[k] for k in ('file','expected_sha256') if k in params}})['params'])
    if command in ('material.preview','texture.bake'):
        from node_contract import PREVIEW,BAKE,validate as validate_nodes
        validate_nodes(params,PREVIEW if command=='material.preview' else BAKE)
        params.update(validate({'schema_version':SCHEMA,'command':'query','params':{k:params[k] for k in ('file','expected_sha256')}})['params'])
    if command in ('node.prepare','material.preview','texture.bake','render.run','media.export','exchange.export','exchange.analyze','extension.run','simulation.bake') and 'resources' in params:
        from node_contract import FILE,array,validate as validate_nodes,file_contract
        validate_nodes(params['resources'],array(FILE,0,100))
        for resource in params['resources']:
            if command in ('render.run','media.export','exchange.export','exchange.analyze','extension.run'):
                from render_contract import file_input
                file_input(resource)
            else:file_contract(resource)
    if command=='asset.plan':
        from library import normalize
        params={'manifest':normalize(params.get('manifest'))}
    if command.startswith("project.plan_"):
        from transactions import normalize_item
        if command == "project.plan_copy":
            params = normalize_item(params)
        else:
            items = params.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                raise Failure("INVALID_REQUEST", "items must be a list of 1..100 copy items")
            params = {"items": [normalize_item(item, mixed=command == "project.plan_files") for item in items]}
    if command.startswith("transaction."):
        import re
        ident = params.get("transaction_id")
        if not isinstance(ident, str) or not re.fullmatch("[0-9a-f]{32}", ident):
            raise Failure("INVALID_REQUEST", "transaction_id must be 32 lowercase hex characters")
    if command in ("inspect", "query", "diff", "dependency.audit", "validate", "identity.prepare", "asset.prepare", "asset.index", "scene.inspect", "model.inspect", "node.inspect", "rig.inspect", "animation.sample", "simulation.inspect"):
        value = params.get("file")
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise Failure("INVALID_REQUEST", "inspect requires a file path")
        path = Path(value).resolve()
        if not path.is_file():
            raise Failure("NOT_FOUND", f"Input file not found: {path}")
        if path.suffix.lower() != ".blend":
            raise Failure("INVALID_REQUEST", "inspect accepts .blend files only")
        params["file"] = str(path)
        if command.startswith("asset."):
            if "expected_sha256" not in params:
                raise Failure("INVALID_REQUEST", "Asset operations require expected_sha256")
            from asset_contract import normalize
            params["manifest"] = normalize(params.get("manifest"), command, str(path))
        if "profile" in params:
            from verification_contract import validate_profile
            if isinstance(params['profile'],dict) and 'closure' in params['profile'] and command!='dependency.audit':
                raise Failure('INVALID_REQUEST','Typed dependency closure requires dependency.audit')
            validate_profile(params["profile"])
            if command != "validate" and set(params["profile"]) & {"evaluate", "render", "asset_records", "asset_index", "catalogs", "require_complete"}:
                raise Failure("INVALID_REQUEST", "Evaluation and index checks require validate command")
            for collection, field in (("resources", "owner"), ("evaluate", "selector")):
                for item in params["profile"].get(collection, []):
                    item[field] = validate({"schema_version": SCHEMA, "command": "query", "params": {"file": str(path), "selector": item[field]}})["params"]["selector"]
        if command == "identity.prepare":
            if "expected_sha256" not in params:
                raise Failure("INVALID_REQUEST", "ID registration requires expected_sha256")
            selectors = params.get("selectors")
            if not isinstance(selectors, list) or not 1 <= len(selectors) <= 100:
                raise Failure("INVALID_REQUEST", "Registration requires 1..100 selectors")
            params["selectors"] = [validate({"schema_version": SCHEMA, "command": "query", "params": {"file": str(path), "selector": s}})["params"]["selector"] for s in selectors]
        if command == "diff":
            params["other"] = validate({"schema_version": SCHEMA, "command": "inspect", "params": {"file": params.get("other")}})["params"]["file"]
        import re
        for key in ("expected_sha256", "other_sha256"):
            if key in params and (not isinstance(params[key], str) or not re.fullmatch("[0-9a-f]{64}", params[key])):
                raise Failure("INVALID_REQUEST", f"{key} must be lowercase SHA-256")
        if "selector" in params:
            selector = params["selector"]
            if not isinstance(selector, dict) or set(selector) not in ({"type", "name", "library"}, {"asset_id"}):
                raise Failure("INVALID_REQUEST", "selector requires exactly type/name/library or asset_id")
            for key, value in selector.items():
                if key == "library" and value is None:
                    continue
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    raise Failure("INVALID_REQUEST", "Invalid selector field")
            if "asset_id" in selector:
                try:
                    normalized = str(uuid.UUID(selector["asset_id"]))
                    if normalized != selector["asset_id"].lower():
                        raise ValueError("Expected hyphenated UUID")
                    selector["asset_id"] = normalized
                except ValueError as exc:
                    raise Failure("INVALID_REQUEST", "asset_id must be a UUID") from exc
            elif selector["library"] is not None:
                if not Path(selector["library"]).is_absolute():
                    raise Failure("INVALID_REQUEST", "selector library must be an absolute path or null for local IDs")
                selector["library"] = os.path.normcase(str(Path(selector["library"]).resolve()))
    if command == "probe" and params.get("case") not in ("scene", "geometry", "nodes", "animation", "render", "exchange"):
        raise Failure("INVALID_REQUEST", "probe case must be scene/geometry/nodes/animation/render/exchange")
    return {"schema_version": SCHEMA, "command": command, "params": params}


def timeout_value(value):
    if not math.isfinite(value) or value <= 0 or value > 86400:
        raise Failure("INVALID_REQUEST", "timeout must be finite and in (0, 86400]")
    return value


def envelope(command=None, job_id=None, data=None, error=None, artifacts=None):
    return {"schema_version": SCHEMA, "cli_version": VERSION, "command": command,
            "job_id": job_id, "ok": error is None, "data": data,
            "error": error, "artifacts": artifacts or {}}
