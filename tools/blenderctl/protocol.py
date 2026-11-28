"""Versioned, strict request/result protocol. Standard library only."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

VERSION = "1.0.0"
SCHEMA = "1.0"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BLENDER = ROOT / "runtime/blender/releases/5.3.0/blender-5.3.0-windows-x64/blender.exe"
DEFAULT_JOBS = ROOT / "runtime/blenderctl/jobs"
DEFAULT_TRANSACTIONS = ROOT / "runtime/blenderctl/transactions"
CODES = {"INVALID_REQUEST": 2, "NOT_FOUND": 3, "CONFLICT": 4,
         "WORKER_FAILED": 5, "TIMEOUT": 6, "CANCELLED": 7,
         "VALIDATION_FAILED": 8, "IO_ERROR": 9, "UNSUPPORTED": 10, "RESOURCE_LIMIT":11, "RATE_LIMITED":12, "ARTIFACT_UNAVAILABLE":13}
HOST_COMMANDS = ("pipeline.plan", "pipeline.run", "pipeline.resume", "asset.plan", "project.plan_copy", "project.plan_batch", "project.plan_files", "transaction.apply", "transaction.recover", "transaction.rollback", "transaction.status")
COMMANDS = ("doctor", "capabilities", "inspect", "query", "diff", "dependency.audit", "validate", "identity.prepare", "asset.prepare", "asset.index", "scene.prepare", "scene.inspect", "model.prepare", "model.inspect", "node.prepare", "node.inspect", "material.preview", "texture.bake", "rig.prepare", "rig.inspect", "rig.package", "animation.sample", "simulation.prepare", "simulation.inspect", "simulation.bake", "render.devices", "render.run", "media.prepare", "media.export", "exchange.formats", "exchange.analyze", "exchange.export", "exchange.import", "exchange.convert", "extension.inspect", "extension.prepare", "extension.run", "probe", *HOST_COMMANDS)


HOST_COMMANDS = (*HOST_COMMANDS, 'asset.preview.batch')
COMMANDS = (*COMMANDS, 'asset.preview.prepare', 'asset.preview.batch', 'dependency.plan-relink', 'dependency.prepare', 'project.link.prepare', 'project.override.prepare', 'project.override.resync')
COMMANDS = (*COMMANDS, 'tracking.prepare', 'tracking.inspect', 'tracking.solve')
COMMANDS = (*COMMANDS, 'project.verify', 'project.prepare_copy')
OPERATIONS=('resource.init','resource.status','cache.inspect','cache.plan_clean','project.package','doctor.verify_install')
REVIEWS=('extension.review','extension.approve')
HOST_COMMANDS=(*HOST_COMMANDS,*REVIEWS)
COMMANDS=(*COMMANDS,*REVIEWS,'sculpt.review')
HOST_COMMANDS=(*HOST_COMMANDS,*OPERATIONS)
COMMANDS=(*COMMANDS,*OPERATIONS)
COMMANDS=(*COMMANDS,'material.run','material.template-save','material.batch','material.study')
COMMANDS=(*COMMANDS,'material.procedural-run','material.procedural-export')
COMMANDS=(*COMMANDS,'material.projection-bake')
HOST_COMMANDS=(*HOST_COMMANDS,'geometry.session')
COMMANDS=(*COMMANDS,'geometry.run','geometry.inspect','geometry.session')
COMMANDS=(*COMMANDS,'lighting.run','lighting.inspect','lighting.template-save','lighting.template-edit','lighting.batch')
COMMANDS=(*COMMANDS,'uv.run','uv.inspect','uv.grid-transfer')
COMMANDS=(*COMMANDS,'uv.batch','uv.batch-resume')
HOST_COMMANDS=(*HOST_COMMANDS,'uv.batch','uv.batch-resume')
COMMANDS=(*COMMANDS,'compositing.run','compositing.inspect','compositing.encode','compositing.template-save','compositing.template-run','compositing.batch','compositing.study')
HOST_COMMANDS=(*HOST_COMMANDS,'compositing.encode')
DEPTH_COMMANDS=('compositing.depth-prepare','compositing.depth-inspect')
COMMANDS=(*COMMANDS,*DEPTH_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,*DEPTH_COMMANDS)
RELIGHT_COMMANDS=('compositing.depth-relight',)
COMMANDS=(*COMMANDS,*RELIGHT_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,*RELIGHT_COMMANDS)
SESSION_COMMANDS=('compositing.depth-session',)
COMMANDS=(*COMMANDS,*SESSION_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,*SESSION_COMMANDS)
ESTIMATE_COMMANDS=('compositing.depth-estimate',)
COMMANDS=(*COMMANDS,*ESTIMATE_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,*ESTIMATE_COMMANDS)
SEQUENCE_COMMANDS=('compositing.depth-sequence',)
COMMANDS=(*COMMANDS,*SEQUENCE_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,*SEQUENCE_COMMANDS)
HOST_COMMANDS=(*HOST_COMMANDS,'workflow.preflight','workflow.run','workflow.resume')
COMMANDS=(*COMMANDS,'workflow.preflight','workflow.run','workflow.resume')
COMMANDS=(*COMMANDS,'modeling.run')
COMMANDS=(*COMMANDS,'toon.run','toon.inspect','toon.template-save','toon.template-edit','toon.template-apply')
HOST_COMMANDS=(*HOST_COMMANDS,'modeling.run')
COMMANDS=(*COMMANDS,'toon-image.run','toon-image.inspect')
HOST_COMMANDS=(*HOST_COMMANDS,'toon-image.run','toon-image.inspect')
COMMANDS=(*COMMANDS,'rigging-workflow.run','rigging-workflow.session')
HOST_COMMANDS=(*HOST_COMMANDS,'rigging-workflow.session')
COMMANDS=(*COMMANDS,'animation.inspect','animation.run')
COMMANDS=(*COMMANDS,'animation.clip-apply')
COMMANDS=(*COMMANDS,'production.run','qa.inspect','video.run','video.inspect','camera.inspect','modeling.import-local','hair.run')


def resource_schema(command):
    """Single public resource-list shape shared by execution and preflight."""
    from node_contract import FILE, array
    return array(FILE, 0, 100)

class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def read_json(path, *, expected_sha256=None):
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
            data = read_shared_bytes(path)
        else:
            data = Path(path).read_bytes()
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise Failure("CONFLICT", f"JSON file SHA-256 changed: {path}")
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs)
    except FileNotFoundError as exc:
        raise Failure("NOT_FOUND", f"JSON file not found: {path}") from exc
    except (ValueError, UnicodeError) as exc:
        raise Failure("INVALID_REQUEST", f"Invalid UTF-8 JSON: {exc}") from exc


def atomic_json(path, value):
    path = Path(path)
    # Same-directory unique O_EXCL file preserves atomic replacement without
    # inflating a legal Windows destination by its entire basename plus UUID.
    temp = path.with_name("." + uuid.uuid4().hex[:12] + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Windows can briefly retain a delete-pending destination while shared
    # readers finish the old JSON generation. Retry only this metadata operation,
    # never a transaction publish or a failed worker. Keep a hard time bound.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(temp, path)
            break
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(.015)


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
    if command == 'hair.run':
        from hair_public import normalize
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize(params)}
    if command in ('video.run', 'video.inspect'):
        from video_workflow_contract import normalize_command
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_command(command, params)}
    if command == 'qa.inspect':
        from qa_contract import normalize_params
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_params(params)}
    if command == 'production.run':
        from production_contract import normalize_params
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_params(params)}
    if command == 'rigging-workflow.run':
        from rigging_workflow_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params)}
    if command == 'rigging-workflow.session':
        if not isinstance(params,dict):
            raise Failure('INVALID_REQUEST','Session params must be an object')
        return {'schema_version':SCHEMA,'command':command,'params':params}
    if command in ('animation.inspect', 'animation.run'):
        from animation_workflow_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,
                'params':normalize_params(params)}
    if command == 'animation.clip-apply':
        from animation_reuse_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,
                'params':normalize_params(params)}
    if command == 'toon-image.run':
        from toon_image_public import normalize
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize(params)}
    if command == 'toon-image.inspect':
        from toon_image_public import normalize_inspect
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_inspect(params)}
    if command == 'camera.inspect':
        from pipeline_camera import normalize
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize(params)}
    if command == 'modeling.import-local':
        from pipeline_modeling_import import normalize
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize(params)}
    if command == 'modeling.run':
        from modeling_workflow_contract import normalize_params
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_params(params)}
    if command == 'workflow.preflight':
        if not isinstance(params, dict) or set(params) != {'requests'}:
            raise Failure('INVALID_REQUEST', 'Preflight requires requests')
        if not isinstance(params['requests'], list) or not 1 <= len(params['requests']) <= 32:
            raise Failure('INVALID_REQUEST', 'Preflight requires 1..32 requests')
        if any(not isinstance(row, dict) or str(row.get('command', '')).startswith('workflow.') for row in params['requests']):
            raise Failure('INVALID_REQUEST', 'Preflight requests must be non-workflow objects')
        return {'schema_version': SCHEMA, 'command': command, 'params': params}
    if command in ('workflow.run', 'workflow.resume'):
        from workflow_contract import normalize_request
        return {'schema_version': SCHEMA, 'command': command, 'params': normalize_request(command, params)}
    if command in SEQUENCE_COMMANDS:
        from compositing_relight_sequence_contract import normalize_params,DepthError
        try:normalized=normalize_params(params)
        except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in SESSION_COMMANDS:
        from compositing_relight_session_contract import normalize_params,DepthError
        try:normalized=normalize_params(params)
        except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in RELIGHT_COMMANDS:
        from compositing_relight_contract import normalize_params,DepthError
        try:normalized=normalize_params(params)
        except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in ESTIMATE_COMMANDS:
        from compositing_depth_estimate_contract import normalize_params,DepthError
        try:normalized=normalize_params(params)
        except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in DEPTH_COMMANDS:
        from compositing_depth_contract import normalize_params,DepthError
        try:normalized=normalize_params(params,command.split('.')[1])
        except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command=='compositing.encode':
        from compositing_encode_public import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(params)}
    if command.startswith('compositing.'):
        from compositing_workflow_contract import normalize_command
        return {'schema_version':SCHEMA,'command':command,'params':normalize_command(command,params)}
    if command in ('uv.batch','uv.batch-resume'):
        from uv_batch import normalize_request
        return {'schema_version':SCHEMA,'command':command,'params':normalize_request(command,params)}
    if command in ('uv.run','uv.inspect','uv.grid-transfer'):
        from uv_workflow_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params,command)}
    if command.startswith('lighting.'):
        from lighting_workflow_contract import normalize_command
        normalized = normalize_command(command, params)
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in ('toon.template-save','toon.template-edit','toon.template-apply'):
        from toon_template_contract import normalize
        return {'schema_version':SCHEMA,'command':command,'params':normalize(command,params)}
    if command in ('toon.run', 'toon.inspect'):
        from toon_workflow_contract import normalize_params, normalize_inspect_params
        normalized = normalize_params(params) if command == 'toon.run' else normalize_inspect_params(params)
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command in ('geometry.run', 'geometry.inspect'):
        from geometry_workflow_contract import normalize_params, normalize_inspect_params
        normalized = normalize_params(params) if command == 'geometry.run' else normalize_inspect_params(params)
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command == 'geometry.session':
        if not isinstance(params, dict) or set(params) - {'session_dir','action','manifest','resources','baseline','candidate','report','request_id','wait_seconds','session_id'}:
            raise Failure('INVALID_REQUEST','Invalid geometry session request')
        return {'schema_version':SCHEMA,'command':command,'params':params}
    if command in ('material.procedural-run','material.procedural-export'):
        from material_procedural_contract import normalize_params, normalize_export_params
        normalized = normalize_params(params) if command=='material.procedural-run' else normalize_export_params(params)
        return {'schema_version':SCHEMA,'command':command,'params':normalized}
    if command=='material.projection-bake':
        from material_projection_contract import normalize_params
        return {'schema_version':SCHEMA,'command':command,'params':normalize_params(params)}
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
               "pipeline.plan":{'manifest'}, "pipeline.run":{'manifest','reuse_job','strict_rehash'}, "pipeline.resume":{'job_id','strict_rehash'},
               "query": {"file", "selector", "expected_sha256", "profile"},
               "diff": {"file", "other", "expected_sha256", "other_sha256", "profile"},
               "dependency.audit": {"file", "expected_sha256", "profile"}, "validate": {"file", "expected_sha256", "profile"},
               "dependency.plan-relink": {"file", "expected_sha256", "manifest"}, "dependency.prepare": {"file", "expected_sha256", "plan"},
               "identity.prepare": {"file", "expected_sha256", "selectors"},
               "asset.prepare": {"file", "expected_sha256", "manifest"}, "asset.index": {"file", "expected_sha256", "manifest"},
               "asset.plan": {"manifest", "force"},
               "asset.preview.prepare": {'file','expected_sha256','manifest','resources','driver_profile','simulation_receipt','reuse_receipt'},
               "asset.preview.batch": {'manifest','reuse_job'},
               "scene.prepare": {"file","expected_sha256","manifest"}, "scene.inspect": {"file","expected_sha256","context"},
               "model.prepare": {"file","expected_sha256","manifest","flow_selection"}, "model.inspect": {"file","expected_sha256","context","qa","flow_selection","review_grid","geometry_audit","face_flow","surface_sweep"},
               "node.prepare": {"file","expected_sha256","manifest","resources"}, "node.inspect": {"file","expected_sha256","context"},
               "rig.prepare": {"file","expected_sha256","manifest","driver_profile"}, "rig.inspect": {"file","expected_sha256","context","suggest_mapping"}, "animation.sample": {"file","expected_sha256","manifest","driver_profile"},
               "rig.package": {"file","expected_sha256","manifest","driver_profile","simulation_receipt"},
               "simulation.prepare": {"file","expected_sha256","manifest","driver_profile"}, "simulation.inspect": {"file","expected_sha256","context","hair_frames","driver_profile"}, "simulation.bake": {"file","expected_sha256","manifest","driver_profile","resources"},
               "material.preview": {"file","expected_sha256","material","resolution","samples","frame","resources"},
               "texture.bake": {"file","expected_sha256","object","scene","view_layer","uv_layer","type","resolution","samples","margin","frame","resources"},
               "exchange.formats":set(), "exchange.analyze":{"file","expected_sha256","manifest","resources","driver_profile","simulation_receipt"}, "exchange.export":{"file","expected_sha256","manifest","resources","driver_profile","simulation_receipt"}, "exchange.import":{"file","expected_sha256","manifest","resources"}, "exchange.convert":{"manifest"}, "extension.inspect":set(), "extension.prepare":{"manifest"}, "extension.run":{"manifest","resources","review_receipt"},
               "render.devices": set(), "render.run": {"file","expected_sha256","manifest","resources","simulation_receipt","driver_profile","cache_manifest"},
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
        from exchange_contract import normalize,descriptor,SHARED_RIG_IMPORT_ADAPTER
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
            shared_native=command=='exchange.import' and params['manifest'].get('rig_adapter')==SHARED_RIG_IMPORT_ADAPTER
            input_limit=1024**3 if native_cache else 512*1024**2 if shared_native else 256*1024**2
            descriptor({k:params[k] for k in ('file','expected_sha256')},max_bytes=input_limit);params['file']=str(Path(params['file']).resolve())
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
        if command=='render.run' and 'cache_manifest' in params:
            from node_contract import FILE,validate as validate_nodes
            from render_contract import file_input
            validate_nodes(params['cache_manifest'],FILE);file_input(params['cache_manifest'])
    if command in ('scene.inspect','model.inspect','node.inspect','rig.inspect','simulation.inspect') and 'context' in params:
        from scene_contract import CONTEXT,validate as validate_scene
        validate_scene(params['context'],CONTEXT)
    if command=='model.inspect' and 'qa' in params:
        from model_contract import normalize_qa
        params['qa']=normalize_qa(params['qa'])
    if command in ('model.inspect', 'model.prepare') and 'flow_selection' in params:
        from flow_contract import LOOKUP
        from scene_contract import validate as validate_scene
        validate_scene(params['flow_selection'], LOOKUP)
    if command=='model.inspect' and 'review_grid' in params:
        from flow_contract import normalize_review_grid
        params['review_grid']=normalize_review_grid(params['review_grid'])
    if command=='model.inspect' and 'face_flow' in params:
        from flow_contract import normalize_face_flow
        if not params.get('expected_sha256'):
            raise Failure('INVALID_REQUEST', 'Face-flow inspection requires expected_sha256')
        params['face_flow']=normalize_face_flow(params['face_flow'])
    if command=='model.inspect' and 'surface_sweep' in params:
        from flow_contract import SURFACE_SWEEP
        from scene_contract import validate as validate_scene
        if not params.get('expected_sha256'):
            raise Failure('INVALID_REQUEST', 'Surface-sweep inspection requires expected_sha256')
        validate_scene(params['surface_sweep'], SURFACE_SWEEP, '$.params.surface_sweep')
    if command=='model.inspect' and 'geometry_audit' in params:
        from flow_contract import GEOMETRY_AUDIT
        from scene_contract import validate as validate_scene
        validate_scene(params['geometry_audit'], GEOMETRY_AUDIT)
    if command=='rig.inspect' and 'suggest_mapping' in params:
        from scene_contract import obj,NAME,validate as validate_scene
        validate_scene(params['suggest_mapping'],obj({'source':NAME,'target':NAME}))
    if command=='simulation.inspect' and 'hair_frames' in params:
        from hair_compat.hair_contract import FRAMES
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
    if command in ('node.prepare','material.preview','texture.bake','render.run','media.export','exchange.export','exchange.import','exchange.analyze','extension.run','simulation.bake') and 'resources' in params:
        from node_contract import FILE,array,validate as validate_nodes,file_contract
        validate_nodes(params['resources'],resource_schema(command))
        for resource in params['resources']:
            if command in ('render.run','media.export','exchange.export','exchange.import','exchange.analyze','extension.run'):
                from render_contract import file_input
                file_input(resource)
            else:file_contract(resource)
    if command=='asset.plan':
        from library import normalize
        force = params.get('force', False)
        if type(force) is not bool:
            raise Failure('INVALID_REQUEST', 'force must be boolean')
        params={'manifest':normalize(params.get('manifest'))}
        if force:
            params['force'] = True
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
