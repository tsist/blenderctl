"""One supervisor per job; immutable request, atomic status, separate cancellation signal."""
import os
from contextlib import ExitStack
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import uuid

from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, HOST_COMMANDS, OPERATIONS, Failure, atomic_json,
                      DEPTH_COMMANDS,ESTIMATE_COMMANDS,RELIGHT_COMMANDS,SESSION_COMMANDS,SEQUENCE_COMMANDS,digest, envelope, read_json, timeout_value, validate)
from processes import ProcessTree


def job_path(root, job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise Failure("INVALID_REQUEST", "job ID must be 32 lowercase hex characters")
    root = Path(root).resolve()
    path = (root / job_id).resolve()
    if path.parent != root:
        raise Failure("INVALID_REQUEST", "Job path leaves jobs directory")
    if not path.is_dir():
        raise Failure("NOT_FOUND", f"Unknown job: {job_id}")
    return path


def prepare(request, blender=DEFAULT_BLENDER, root=DEFAULT_JOBS, timeout=120, transactions_root=DEFAULT_TRANSACTIONS, internal=False, limits=None, resource_domain=None):
    if internal:
        if request.get("command") not in ("_save_copy", "_inspect_staged", "_library_validate"):
            raise Failure("INVALID_REQUEST", "Unknown internal command")
    else:
        request = validate(request)
    timeout_value(timeout)
    image_identity = None
    if request['command'] == 'toon-image.run':
        from toon_image_public import prepare_launch
        blender = Path(blender).resolve()
        image_identity = prepare_launch(request['params'], str(blender), timeout, limits, resource_domain)
    if request['command'] == 'toon-image.inspect':
        from toon_image_public import prepare_inspect_launch
        image_identity = prepare_inspect_launch(request['params'], timeout, limits, resource_domain)
    if request['command'] in SEQUENCE_COMMANDS:
        if limits is not None:raise Failure('UNSUPPORTED','Sequence has explicit frame/pixel/time budgets; OS RAM/VRAM limits are not qualified')
        budget=request['params'].get('budget',{}).get('timeout_seconds',30)
        if timeout<budget+90:raise Failure('INVALID_REQUEST','Sequence parent timeout must cover worker budget plus90s cleanup allowance')
    if request['command'] in SESSION_COMMANDS:
        if limits is not None:raise Failure('UNSUPPORTED','Hot session has explicit lifetime/pixel/time/work budgets, not OS RAM/VRAM limits')
        params=request['params']
        if timeout<params['lifetime_seconds']+params['relight']['budget']['timeout_seconds']+90:
            raise Failure('INVALID_REQUEST','Session launch timeout must cover lifetime and numeric stage budget plus90s runtime/cleanup allowance')
    if request['command'] in RELIGHT_COMMANDS:
        if limits is not None:raise Failure('UNSUPPORTED','Relight has explicit pixel/time/work budgets, not OS RAM/VRAM limits')
        if timeout<request['params']['budget']['timeout_seconds']+90:
            raise Failure('INVALID_REQUEST','Relight launch timeout must cover numeric stage budget plus90s runtime/cleanup allowance')
    if request['command'] in ESTIMATE_COMMANDS:
        if limits is not None:raise Failure('UNSUPPORTED','DAV2 Host inference does not certify RAM/VRAM limits')
        params=request['params'];budget=params['budget']
        if params['domain']['width']*params['domain']['height']>budget['max_pixels']:
            raise Failure('RESOURCE_LIMIT','Estimate original RGB exceeds its declared pixel budget')
        if timeout<budget['timeout_seconds']+90:
            raise Failure('INVALID_REQUEST','Estimate launch timeout must cover raw worker budget plus90s preimport/cleanup allowance')
    if request['command'] in DEPTH_COMMANDS:
        if limits is not None:raise Failure('UNSUPPORTED','Depth Host work units do not certify RAM/VRAM limits')
        if request['command']=='compositing.depth-prepare' and timeout<request['params']['budget']['timeout_seconds']+30:
            raise Failure('INVALID_REQUEST','Depth launch timeout must cover the explicit preparation budget plus30s cleanup grace')
        if request['command']=='compositing.depth-inspect' and timeout<30:
            raise Failure('INVALID_REQUEST','Depth cold inspection launch timeout must be at least30s')
    if request["command"]=="compositing.encode" and limits is not None:
        raise Failure("UNSUPPORTED","M4 only has explicit pixel/time budgets; Host Job RAM/VRAM integration not qualified")
    if limits is not None:
        from pipeline_contract import LIMITS
        from scene_contract import validate as validate_limits
        validate_limits(limits,LIMITS)
    if request["command"]=="compositing.encode" and timeout<request["params"]["budget"]["timeout_seconds"]+30:
        raise Failure("INVALID_REQUEST","M4 launch timeout must cover explicit worker budget plus30s cleanup grace")
    blender = Path(blender).absolute() if request['command'] == 'toon-image.inspect' else Path(blender).resolve()
    modeling_host=request['command']=='modeling.run' and request['params']['manifest']['operation']!='import_local'
    if request['command'] != 'toon-image.inspect' and not blender.is_file() and not modeling_host and request["command"] not in (*OPERATIONS,*DEPTH_COMMANDS,*ESTIMATE_COMMANDS,*RELIGHT_COMMANDS,*SESSION_COMMANDS,*SEQUENCE_COMMANDS,"transaction.status", "transaction.rollback", "transaction.recover", "compositing.encode"):
        raise Failure("NOT_FOUND", f"Blender executable not found: {blender}")
    production_binding = {}
    if request['command'] == 'production.run':
        from production_contract import launch_binding
        production_binding = launch_binding(request['params'])
    job = Path(root).resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    atomic_json(job / "request.json", request)
    video_binding = {"video_binding": "registry"} if request["command"] in ("video.inspect", "video.run") else {}
    atomic_json(job / "launch.json", {**video_binding, **production_binding, "blender": str(blender), "timeout_seconds": timeout,
                                     "transactions_root": str(Path(transactions_root).resolve()),**({'limits':limits} if limits else {}),**({'resource_domain':resource_domain} if resource_domain else {})})
    atomic_json(job / "status.json", {"job_id": job.name, "state": "queued", "created_at": time.time(), "heartbeat_at": time.time()})
    if request['command'] in ('animation.inspect', 'animation.run',
                              'animation.clip-apply'):
        launch = read_json(job / 'launch.json')
        launch['animation_identity'] = request['params']['implementation_sha256']
        atomic_json(job / 'launch.json', launch)
    if image_identity is not None:
        from toon_image_public import canonical
        import hashlib
        launch = read_json(job / 'launch.json')
        launch.update(image_identity=image_identity, image_request_sha256=hashlib.sha256(canonical(request)).hexdigest())
        atomic_json(job / 'launch.json', launch)
    if request["command"]=="compositing.encode":
        from compositing_encode_public import launch_identity
        launch=read_json(job/"launch.json")
        launch.update(encode_identity=launch_identity(),encode_request_signature=__import__("compositing_encode_contract").signature(request["params"]))
        atomic_json(job/"launch.json",launch)
    if request['command'] in DEPTH_COMMANDS:
        from compositing_depth_public import launch_identity
        from compositing_depth_contract import signature
        launch=read_json(job/'launch.json')
        launch.update(depth_identity=launch_identity(),depth_request_signature=signature(request['params']))
        atomic_json(job/'launch.json',launch)
    if request['command'] in SESSION_COMMANDS:
        from compositing_relight_session_public import launch_identity
        from compositing_relight_session_contract import signature
        launch=read_json(job/'launch.json')
        launch.update(session_identity=launch_identity(),session_request_signature=signature(request['params']))
        atomic_json(job/'launch.json',launch)
    if request['command'] in RELIGHT_COMMANDS:
        from compositing_relight_public import launch_identity
        from compositing_relight_contract import signature
        launch=read_json(job/'launch.json')
        launch.update(relight_identity=launch_identity(),relight_request_signature=signature(request['params']))
        atomic_json(job/'launch.json',launch)
    if request['command'] in ESTIMATE_COMMANDS:
        from compositing_depth_estimate_public import launch_identity
        from compositing_depth_estimate_contract import signature
        launch=read_json(job/'launch.json')
        launch.update(estimate_identity=launch_identity(),estimate_request_signature=signature(request['params']))
        atomic_json(job/'launch.json',launch)
    if request['command'] in SEQUENCE_COMMANDS:
        from compositing_relight_sequence_public import launch_identity
        from compositing_relight_sequence_contract import signature
        launch=read_json(job/'launch.json')
        launch.update(sequence_identity=launch_identity(),sequence_request_signature=signature(request['params']))
        atomic_json(job/'launch.json',launch)
    return job


def submit(job):
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    with (job / "supervisor.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), str(job)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, start_new_session=os.name != "nt", close_fds=True)
    return envelope("job.submit", job.name, {"state": "queued"}, artifacts={"job_directory": str(job)})


def status(job):
    value = read_json(job / "status.json")
    if value["state"] in ("queued", "running") and time.time() - value["heartbeat_at"] > 15:
        value["health"] = "stale_supervisor_review_required"
    else:
        value["health"] = "current"
    rigging_progress = job / 'rigging-progress.json'
    if rigging_progress.is_file():
        try:
            import json
            import math
            if rigging_progress.stat().st_size > 16384:
                raise Failure('RESOURCE_LIMIT', 'Rigging progress exceeds16KiB')
            progress = read_json(rigging_progress)
            fields = {'schema','job_id','sequence','timestamp','phase','step_id','operation','details'}
            if (not isinstance(progress,dict) or set(progress)!=fields
                    or progress['schema']!='rigging.progress/1.0' or progress['job_id']!=job.name
                    or type(progress['sequence']) is not int or progress['sequence']<1
                    or type(progress['timestamp']) not in (int,float) or not math.isfinite(progress['timestamp'])
                    or not isinstance(progress['phase'],str) or not 1<=len(progress['phase'])<=128
                    or not all(item is None or isinstance(item,str) and len(item)<=128 for item in (progress['step_id'],progress['operation']))
                    or not isinstance(progress['details'],dict)):
                raise Failure('INVALID_REQUEST','Invalid or foreign rigging progress')
            json.dumps(progress,allow_nan=False)
            value['progress'] = progress
        except (Failure,OSError,ValueError,TypeError) as error:
            value['progress_error'] = str(error)[:512]
    for filename in ('procedural-progress.json', 'projection-progress.json', 'study-progress.json', 'batch-progress.json', 'workflow-progress.json', 'uv-progress.json'):
        path = job / filename
        if path.is_file():
            try:
                from material_interface import progress_summary
                value['progress'] = progress_summary(read_json(path))
            except (Failure, OSError) as error:
                value['progress_error'] = str(error)[:512]
            break
    return envelope("job.status", job.name, value, artifacts={"job_directory": str(job)})


def wait_for_job(job, seconds=30):
    """Bounded internal polling, one caller return with final result or rich progress."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 55:
        raise Failure('INVALID_REQUEST', 'Job wait must be 0..55 seconds')
    deadline = time.monotonic() + seconds
    while True:
        if (job / 'result.json').is_file():
            return read_json(job / 'result.json')
        observed = status(job)
        if observed['data']['state'] not in ('queued', 'running') or time.monotonic() >= deadline:
            return observed
        time.sleep(min(.25, max(0, deadline - time.monotonic())))


def cancel(job):
    value = status(job)["data"]
    if value["state"] in ("queued", "running"):
        (job / "cancel.request").touch(exist_ok=True)
        value["cancel_requested"] = True
    return envelope("job.cancel", job.name, value, artifacts={"job_directory": str(job)})

def cleanup_isolation(job):
    from isolation_lifecycle import recover
    return envelope('job.cleanup_isolation',job.name,recover(job),artifacts={'job_directory':str(job),'cleanup':str(job/'isolation-cleanup.json')})


def execute_worker(job, parent_checkpoint=None):
    from timing import Trace
    trace = Trace(job / 'timing.json', metadata={'scope': 'host worker lifecycle; render stages have a separate trace',
                                               'job_id': job.name, 'parent_job_id': job.parent.parent.name if job.parent.name == 'children' else None})
    try:
        with trace.span('worker_job_total') as span:
            result, code = _execute_worker(job, parent_checkpoint)
        error = result.get('error')
        if error:
            span['status'] = 'failed'
            span['error'] = error
        trace.finish(Failure(error['code'], error['message']) if error else None)
        result['artifacts']['timing'] = str(job / 'timing.json')
        atomic_json(job / 'result.json', result)
        return result, code
    except BaseException as exc:
        trace.finish(exc)
        raise


def _execute_worker(job, parent_checkpoint=None):
    started = time.monotonic()
    tree = None
    lease = None
    isolation = None
    request = read_json(job / "request.json")
    isolated_requested=request['command']=='extension.run' and 'isolation' in request['params'].get('manifest',{})
    launch = read_json(job / "launch.json")
    state = read_json(job / "status.json")
    state.update(state="running", supervisor_pid=os.getpid(), started_at=time.time())
    source = request["params"].get("file")
    before = None
    error = None
    data = None
    worker_exit = None
    input_guards = ExitStack()
    input_hashes = {}
    artifacts = {"job_directory": str(job), "request": str(job / "request.json"),
                 "stdout_log": str(job / "stdout.log"), "stderr_log": str(job / "stderr.log")}
    limits=launch.get('limits');usage={};last_resource_check=0;memory_observations=[]
    def checkpoint():
        nonlocal last_resource_check
        if parent_checkpoint:
            parent_checkpoint()
        if (job / "cancel.request").exists():
            raise Failure("CANCELLED", "Cancelled by request")
        if time.monotonic() - started > launch["timeout_seconds"]:
            raise Failure("TIMEOUT", "Job exceeded timeout")
        if limits and time.monotonic()-last_resource_check>=.25:
            from pipeline_contract import disk_usage
            size=disk_usage(job);last_resource_check=time.monotonic();usage['observed_disk_bytes']=max(size,usage.get('observed_disk_bytes',0))
            if tree:
                usage.update(tree.usage())
                from telemetry import memory_sample
                sample=memory_sample(tree);sample['elapsed_seconds']=time.monotonic()-started
                if len(memory_observations)<4096:memory_observations.append(sample)
            if size>limits['disk_mb']*1048576:raise Failure('RESOURCE_LIMIT','Observed child job disk budget exceeded; partial files retained')
        if time.time() - state["heartbeat_at"] >= 1:
            state["heartbeat_at"] = time.time()
            atomic_json(job / "status.json", state)
    try:
        if request['command'] in ('animation.inspect', 'animation.run',
                                  'animation.clip-apply'):
            from animation_identity import implementation_sha256 as animation_identity_sha256
            current_identity = animation_identity_sha256()
            if (request['params'].get('implementation_sha256') != current_identity
                    or launch.get('animation_identity') != current_identity):
                raise Failure('CONFLICT',
                              'Animation implementation changed after request normalization')
        checkpoint()
        atomic_json(job / "status.json", state)
        if request['command'] == 'production.run':
            from filesystem import FileGuard
            from production_contract import implementation_files, implementation_identity
            params = request['params']
            for descriptor in [params, *params['resources']]:
                path = descriptor['file']
                guard = input_guards.enter_context(FileGuard(path))
                input_hashes[path] = guard.sha256(checkpoint)
                if input_hashes[path] != descriptor['expected_sha256']:
                    raise Failure('CONFLICT', f'Production input fingerprint differs from the request: {path}')
            domain = params['domain']
            release = launch.get('production_release', 'workspace')
            files = implementation_files(domain, release)
            protected = {path: input_guards.enter_context(FileGuard(path)).sha256(checkpoint) for path in files}
            if implementation_identity(protected, domain, release) != launch.get('production_implementation_sha256'):
                raise Failure('CONFLICT', 'Production add-on candidate changed while the job was queued')
        if request['command']=='sculpt.review':
            from reviews import protect
            for d in (request['params']['manifest']['before'],request['params']['manifest']['after']):
                protect(d,input_guards,checkpoint)
                input_hashes[d['file']]=d['expected_sha256']
        if request["command"] in ("modeling.run", "modeling.import-local", "camera.inspect", "compositing.run", "compositing.inspect", "compositing.template-save", "compositing.template-run", "compositing.batch", "compositing.study", "lighting.run", "lighting.inspect", "lighting.template-save", "lighting.template-edit", "lighting.batch", "uv.run", "uv.inspect", "uv.grid-transfer", "geometry.run", "geometry.inspect", "toon.run", "toon.inspect", "toon.template-save", "toon.template-edit", "toon.template-apply", "inspect", "project.verify", "project.prepare_copy", "tracking.prepare", "tracking.inspect", "tracking.solve", "asset.preview.prepare", "query", "diff", "dependency.audit", "dependency.plan-relink", "dependency.prepare", "project.link.prepare", "project.override.prepare", "project.override.resync", "validate", "identity.prepare", "asset.prepare", "asset.index", "scene.prepare", "scene.inspect", "model.prepare", "model.inspect", "qa.inspect", "video.run", "video.inspect", "node.prepare", "node.inspect", "material.preview", "material.run", "material.template-save", "material.batch", "material.study", "material.procedural-run", "material.procedural-export", "material.projection-bake", "texture.bake", "rig.prepare", "rig.inspect", "rig.package", "animation.sample", "animation.inspect", "animation.run", "animation.clip-apply", "simulation.prepare", "simulation.inspect", "simulation.bake", "render.run", "media.prepare", "media.export", "exchange.analyze", "exchange.export", "exchange.import", "exchange.convert", "extension.run", "extension.prepare"):
            from filesystem import FileGuard
            for key, expected in (("file", "expected_sha256"), ("other", "other_sha256")):
                filename = request["params"].get(key)
                if not filename:
                    continue
                if filename not in input_hashes:
                    guard = input_guards.enter_context(FileGuard(filename))
                    input_hashes[filename] = guard.sha256(checkpoint)
                if request["params"].get(expected, input_hashes[filename]) != input_hashes[filename]:
                    raise Failure("CONFLICT", f"Input hash differs from expected: {filename}")
            profile = request["params"].get("profile", {})
            documents = profile.get("catalogs", []) + ([profile["asset_index"]] if "asset_index" in profile else [])
            if request['command'] in ('dependency.audit','dependency.plan-relink','dependency.prepare'):
                params=request['params'];anchors=[]
                if profile.get('closure',{}).get('simulation_receipt'):anchors.append(profile['closure']['simulation_receipt'])
                if request['command']=='dependency.plan-relink':anchors.append(params['manifest']['baseline'])
                if request['command']=='dependency.prepare':anchors.append(params['plan'])
                # Lock descriptor bytes before expanding their dependent files.
                for document in anchors:
                    filename=document['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=document['expected_sha256']:raise Failure('CONFLICT','Dependency descriptor hash changed')
                from dependency_contract import input_documents
                documents+=input_documents(params,request['command'])
            if request['command'] in ('project.link.prepare', 'project.override.prepare', 'project.override.resync'):
                params = request['params']
                if request['command'] == 'project.override.resync':
                    # Receipt bytes must be immutable before they enumerate further inputs.
                    document = params['manifest']['receipt']
                    filename = document['file']
                    if filename not in input_hashes:
                        guard = input_guards.enter_context(FileGuard(filename))
                        input_hashes[filename] = guard.sha256(checkpoint)
                    if input_hashes[filename] != document['expected_sha256']:
                        raise Failure('CONFLICT', 'Override receipt hash changed')
                from link_contract import input_documents
                documents += input_documents(params, request['command'])
            if request['command'] in ('project.verify','project.prepare_copy'):
                documents+=request['params']['resources']
            if request['command'].startswith('tracking.'):
                from tracking_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command']=='extension.run' and 'review_receipt' in request['params']:
                from reviews import verify_run
                verify_run(request['params'],launch['blender'],input_guards,checkpoint)
            if request['command']=='extension.prepare':
                from exchange_contract import addon_documents
                documents+=addon_documents(launch['blender'])
            if request['command'] in ('node.prepare','material.preview','texture.bake'):
                from node_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'].startswith('compositing.'):
                from compositing_workflow_contract import anchor_documents, input_documents
                for anchor in anchor_documents(request['params'],request['command']):
                    filename=anchor['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=anchor['expected_sha256']:raise Failure('CONFLICT','Compositing descriptor changed')
                documents+=input_documents(request['params'],request['command'])
            if request['command'] in ('modeling.run', 'modeling.import-local'):
                from modeling_workflow_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('uv.run','uv.inspect','uv.grid-transfer'):
                from uv_workflow_contract import input_documents
                documents+=input_documents(request['params'],request['command'])
            if request['command'] in ('animation.inspect','animation.run'):
                from animation_workflow_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command']=='animation.clip-apply':
                from animation_reuse_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'].startswith('lighting.'):
                from lighting_workflow_contract import anchor_documents, input_documents
                for anchor in anchor_documents(request['params'],request['command']):
                    filename=anchor['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=anchor['expected_sha256']:raise Failure('CONFLICT','Lighting descriptor changed')
                documents+=input_documents(request['params'],request['command'])
            if request['command'] in ('toon.template-save','toon.template-edit','toon.template-apply'):
                from toon_template_contract import anchors, input_documents
                for anchor in anchors(request['params'],request['command']):
                    filename=anchor['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=anchor['expected_sha256']:
                        raise Failure('CONFLICT','Toon template anchor changed')
                documents+=input_documents(request['params'],request['command'])
            if request['command'] in ('toon.run','toon.inspect'):
                from toon_workflow_contract import anchor_documents, input_documents
                for anchor in anchor_documents(request['params']):
                    filename=anchor['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=anchor['expected_sha256']:
                        raise Failure('CONFLICT','Toon baseline anchor changed')
                documents+=input_documents(request['params'])
            if request['command'] in ('geometry.run','geometry.inspect'):
                if request['params'].get('resume'):
                    anchor=request['params']['resume'];filename=anchor['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if input_hashes[filename]!=anchor['expected_sha256']:raise Failure('CONFLICT','Geometry resume descriptor changed')
                from geometry_workflow_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('material.procedural-run','material.procedural-export'):
                from material_procedural_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command']=='material.projection-bake':
                from material_projection_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('material.run','material.template-save'):
                from material_workflow_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('material.batch','material.study'):
                if request['command']=='material.study':
                    from material_study_contract import input_documents
                else:
                    from material_batch_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('model.prepare','rig.prepare'):
                from curve_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('render.run','media.prepare','media.export'):
                from render_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] in ('exchange.analyze','exchange.export','exchange.import','exchange.convert','extension.run'):
                from exchange_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] == 'camera.inspect':
                from pipeline_camera import input_documents
                documents += input_documents(request['params'])
            if request['command'] in ('video.run', 'video.inspect'):
                from video_workflow_contract import input_documents
                documents += input_documents(request['params'], request['command'])
            if request['command']=='asset.preview.prepare':
                from preview_contract import input_documents
                documents+=input_documents(request['params'])
            if request['command'] == 'asset.prepare':
                documents += [t['preview'] for t in request['params']['manifest']['targets'] if 'preview' in t]
            if request['command']=='rig.package' and 'simulation_receipt' in request['params']:
                documents.append(request['params']['simulation_receipt'])
            if request['command']=='simulation.bake':
                from simulation_contract import input_documents
                documents+=input_documents(request['params'])
            for document in documents:
                filename = document.get("file")
                if filename:
                    if filename not in input_hashes:
                        guard = input_guards.enter_context(FileGuard(filename))
                        input_hashes[filename] = guard.sha256(checkpoint)
                    if document["expected_sha256"] != input_hashes[filename]:
                        raise Failure("CONFLICT", "Index/Catalog input hash changed")
            if request['command']=='asset.preview.prepare' and 'reuse_receipt' in request['params']:
                from preview_contract import receipt_inputs
                receipt=read_json(request['params']['reuse_receipt']['file'])
                for document in receipt_inputs(receipt):
                    filename=document['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if document['expected_sha256']!=input_hashes[filename]:raise Failure('CONFLICT','Preview receipt output changed')
            if (request['command']=='simulation.bake' and request['params']['manifest']['mode'] in ('reuse','relocate')) or 'simulation_receipt' in request['params']:
                descriptor=request['params'].get('simulation_receipt') or request['params']['manifest']['receipt']
                receipt=read_json(descriptor['file'])
                from simulation_contract import receipt_contract
                receipt_contract(receipt)
                for document in receipt['files']:
                    filename=document['file']
                    if filename not in input_hashes:
                        guard=input_guards.enter_context(FileGuard(filename));input_hashes[filename]=guard.sha256(checkpoint)
                    if document['expected_sha256']!=input_hashes[filename]:raise Failure('CONFLICT','Simulation cache file hash changed')
        if request['command']=='rigging-workflow.run':
            from rigging_workflow_contract import input_documents
            from filesystem import FileGuard
            documents=[{'file':source,'expected_sha256':request['params']['expected_sha256']}]+input_documents(request['params'])
            for document in documents:
                filename=document['file']
                if filename not in input_hashes:
                    guard=input_guards.enter_context(FileGuard(filename))
                    input_hashes[filename]=guard.sha256(checkpoint)
                if input_hashes[filename]!=document['expected_sha256']:
                    raise Failure('CONFLICT','Rigging input/runtime hash changed: '+filename)
        if source:
            before = digest(source, checkpoint)
        if isolated_requested:
            from script_isolation import IsolatedJob
            isolation=IsolatedJob(job,request,launch,input_guards,checkpoint)
            command=isolation.command;env=isolation.env
        else:
            config = job / "user"
            env = {k: v for k, v in os.environ.items() if not k.startswith("BLENDER_") and k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
            for name in ("CONFIG", "SCRIPTS", "EXTENSIONS", "DATAFILES"):
                path = config / name.lower()
                path.mkdir(parents=True)
                env["BLENDER_USER_" + name] = str(path)
            env["BLENDER_USER_RESOURCES"] = str(config)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            command = [launch["blender"], "--background", "--factory-startup", "--disable-autoexec",
                       "--offline-mode", "--threads", str(limits['cpu_threads'] if limits else 2), "--python-exit-code", "23",
                       "--python", str(Path(__file__).with_name("worker.py")), "--", str(job)]
        atomic_json(job / "command.json", command)
        with (job / "stdout.log").open("wb") as out, (job / "stderr.log").open("wb") as err:
            bootstrap = command if isolation else [sys.executable, str(Path(__file__).with_name("bootstrap.py")), str(job), *command]
            if launch.get('resource_domain'):
                from resource_leases import Lease
                lease=Lease(launch['resource_domain'],limits,job,checkpoint)
                atomic_json(job/'resource-lease.json',{'domain':launch['resource_domain'],'lease_id':lease.id,'job_name':lease.job_name})
            owned_job_name=lease.job_name if lease else (isolation.lifecycle.state['job_name'] if isolation else None)
            if isolation:isolation.lifecycle.set_job(owned_job_name)
            tree = ProcessTree(bootstrap, memory_bytes=limits['memory_mb']*1048576 if limits else None, job_name=owned_job_name, sandbox=isolation.container if isolation else None, cwd=str(isolation.scratch if isolation else job), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            if isolation:isolation.launched(tree.process)
            if lease:lease.started(tree.process.pid,checkpoint)
            state["process_tree_root_pid"] = tree.process.pid
            (job / "worker.gate").touch(exist_ok=False)
            atomic_json(job / "status.json", state)
            while tree.process.poll() is None:
                checkpoint()
                time.sleep(0.1)
            worker_exit = tree.process.returncode
        last_resource_check=0;checkpoint()
        if (job / "cancel.request").exists():
            raise Failure("CANCELLED", "Cancelled before result acceptance")
        if time.monotonic() - started > launch["timeout_seconds"]:
            raise Failure("TIMEOUT", "Worker exceeded job timeout")
        result_file = job / "worker-result.json"
        if isolation:
            if worker_exit!=0:raise Failure('WORKER_FAILED',f'Isolated worker exit={worker_exit}; no unconfined retry')
            accepted=isolation.accept()
            atomic_json(result_file,{'ok':True,'data':accepted,'error':None,'producer':'host_output_receiver'})
        if worker_exit != 0 or not result_file.is_file():
            details = read_json(result_file).get("error") if result_file.is_file() else None
            code = details.get("code", "WORKER_FAILED") if isinstance(details, dict) else "WORKER_FAILED"
            raise Failure(code if code in CODES else "WORKER_FAILED", f"Worker exit={worker_exit}; {details or 'no result'}")
        result = read_json(result_file)
        if not result.get("ok"):
            if request['command'] in ('modeling.run','material.run','material.template-save','material.batch','material.study','material.procedural-run','material.procedural-export','material.projection-bake','geometry.run','geometry.inspect','toon.run','toon.inspect','lighting.run','lighting.inspect','lighting.template-save','lighting.template-edit','lighting.batch','uv.run','uv.inspect','uv.grid-transfer','animation.run','animation.inspect','animation.clip-apply'):
                detail=result.get('error') or {}
                raise Failure(detail.get('code') if detail.get('code') in CODES else 'WORKER_FAILED',str(detail.get('message',detail)))
            raise Failure("VALIDATION_FAILED", str(result.get("error")))
        data = result["data"]
        if request['command'] in ('toon.template-save','toon.template-edit','toon.template-apply'):
            from toon_templates_host import accept
            data=accept(job,request,data,input_guards,checkpoint)
        if request['command'] in ('toon.run','toon.inspect'):
            from toon_host import accept
            data=accept(job,request,data,input_guards,checkpoint)
        if source and digest(source, checkpoint) != before:
            raise Failure("CONFLICT", "Input changed during inspection; result not accepted")
        if source:
            data["source_sha256"] = before
            data["source_unchanged"] = True
        if input_hashes:
            for filename, original in input_hashes.items():
                if digest(filename, checkpoint) != original:
                    raise Failure("CONFLICT", "One of the query/diff inputs changed")
            data["input_hashes"] = input_hashes
        artifacts["worker_result"] = str(result_file)
    except KeyboardInterrupt:
        error = {"code": "CANCELLED", "message": "Interrupted by Ctrl+C"}
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    except Exception as exc:
        error = {"code": "WORKER_FAILED", "message": str(exc)}
        (job / "supervisor-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if request['command'] in ('compositing.template-save','compositing.template-run','compositing.batch','compositing.study'):
            # Own-tree paths only; ancestor paths stay in hashed domain anchors.
            for path in job.rglob('*'):
                if path.is_file():artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] == 'camera.inspect' and (job / 'camera-inspection.json').is_file():
            artifacts['camera-inspection.json'] = str(job / 'camera-inspection.json')
        if request['command'] in ('modeling.run', 'modeling.import-local'):
            for path in job.rglob('*'):
                if path.is_file() and (path.name.startswith('modeling-') or path.suffix.lower() in ('.blend','.png')):
                    artifacts[str(path.relative_to(job))]=str(path)
        if request['command'].startswith('lighting.'):
            for path in job.rglob('*'):
                if path.is_file() and (path.name.startswith('lighting-') or 'previews' in path.parts or 'items' in path.parts or 'resources' in path.parts):
                    artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] in ('uv.run','uv.inspect','uv.grid-transfer'):
            for path in job.glob('uv-*'):
                if path.is_file():artifacts[path.name]=str(path)
        if request['command']=='uv.grid-transfer':
            for name in ('normal.png','receipt.json'):
                output = job / 'grid-transfer' / name
                if output.is_file():artifacts['grid-transfer/'+name]=str(output)
        if request['command'] in ('animation.inspect','animation.run'):
            for name in ('animation-report.json','animation-review.json',
                         'animation-execute.json','animation-after.json',
                         'animation-inspection.json','animation-resource-closure.json',
                         'animation-before-record.json',
                         'animation-record-evidence.json',
                         'animation-verification-request.json','animation-reopen.json',
                         'animation-reopen.log','animation-candidate.blend',
                         'animation-candidate.failed.blend',
                         'animation-candidate.failed.json'):
                if (job/name).is_file():artifacts[name]=str(job/name)
        if request['command']=='animation.clip-apply':
            for path in job.glob('animation-reuse-*'):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(job.resolve()):
                    artifacts[path.name]=str(path)
        if request['command'] in ('toon.run','toon.inspect','toon.template-save','toon.template-edit','toon.template-apply'):
            for path in job.rglob('*'):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(job.resolve()):
                    artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] in ('geometry.run','geometry.inspect'):
            for path in job.glob('geometry-*'):
                if path.is_file():artifacts[path.name]=str(path)
        if request['command'] in ('material.batch','material.study'):
            for path in job.rglob('*'):
                if path.is_file():artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] in ('material.procedural-run','material.procedural-export'):
            for path in job.rglob('*'):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(job.resolve()):
                    artifacts[str(path.relative_to(job))]=str(path)
        if request['command']=='material.projection-bake':
            for path in job.rglob('*'):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(job.resolve()):
                    artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] in ('material.run','material.template-save'):
            for name in ('workflow-report.json','workflow-progress.json','candidate.blend','contact-sheet.png','contact-sheet.index.json','resources.json','manifest.json','source-snapshot.json','candidate-expected.json','template.json','bindings.json'):
                if (job/name).is_file():artifacts[name]=str(job/name)
            for name in ('images','views','textures'):
                for path in (job/name).rglob('*'):
                    if path.is_file():artifacts[str(path.relative_to(job))]=str(path)
        if request['command'] == 'production.run':
            names = ('production-report.json',) if error else ('production-report.json', 'production-candidate.blend')
            for name in names:
                if (job / name).is_file():
                    artifacts[name] = str(job / name)
        if request['command'] in ('project.link.prepare', 'project.override.prepare', 'project.override.resync'):
            for name in ('project-link-report.json', 'project-override-report.json', 'project-override-conflicts.json', 'project-override-receipt.json', 'project-candidate.blend', 'project-before.json', 'project-expected.json', 'project-observed.json', 'dependency-closure.json', 'project-library-closure.json', 'project-source-closure.json', 'project-resync-closure.json', 'project-upstream-changes.json'):
                if (job / name).is_file():artifacts[name] = str(job / name)
        if request['command'].startswith('dependency.'):
            for pattern in ('dependency-*.json','dependency-*.blend'):
                for path in job.glob(pattern):
                    if path.is_file():artifacts[path.name]=str(path)
        if request['command'].startswith(('exchange.','extension.','sculpt.')) and not isolated_requested:
            for path in job.rglob('*'):
                if path.is_file() and (path.name.startswith(('exchange-','extension-','export.','sculpt-')) or path.parent==job and path.name in request['params'].get('manifest',{}).get('outputs',[])):
                    artifacts[str(path.relative_to(job))]=str(path)
        for pattern in ('render-*.json','render-*.png','render-*.jpg','render-*.exr','media-*.json','media-*.blend','media-*.png','video.mp4','audio.wav'):
            for path in job.glob(pattern):
                if path.is_file():artifacts[path.name]=str(path)
        for name in ('simulation-before.json','simulation-expected.json','simulation-report.json','simulation-progress.json','simulation-candidate.blend','simulation-unbaked.blend','simulation-receipt.json','simulation-samples.json','hair-report.json','hair-expected.json'):
            if (job/name).is_file():artifacts[name]=str(job/name)
        for path in job.glob('project-*'):
            if path.is_file():artifacts[path.name]=str(path)
        for path in job.glob('tracking-*'):
            if path.is_file():artifacts[path.name]=str(path)
        if request['command']=='rig.package':
            for path in job.glob('rig-*'):
                if path.is_file():artifacts[path.name]=str(path)
        for name in ('rig-before.json','rig-expected.json','rig-report.json','rig-actions.json','rig-change.json','rig-candidate.blend','animation-samples.json','rig-profile.json','rig-mapping-suggestions.json','rig-adapter-report.json','action-set-progress.json','action-set-quality.json'):
            if (job/name).is_file():artifacts[name]=str(job/name)
        for name in ('node-before.json','node-expected.json','node-report.json','node-actions.json','node-change.json','node-candidate.blend','material-preservation.json','material-preview.png','material-preview.json','baked-texture.png','bake-report.json'):
            if (job/name).is_file():artifacts[name]=str(job/name)
        for name in ('model-before.json','model-expected.json','model-report.json','model-actions.json','model-change.json','model-candidate.blend','uv-layout-report.json','model-evaluation-reopen.json','geometry-operation-report.json','hard-surface-report.json','flow-selection.json','flow-selection-recovery.json','face-flow-report.json','surface-sweep-report.json','geometry-audit.json','review-grid.png','review-grid.json'):
            if (job/name).is_file():artifacts[name]=str(job/name)
        for name in ("snapshot.json", "before.snapshot.json", "after.snapshot.json", "validation.json", "dependencies.json", "evaluation.json", "validation-render.png", "identity-map.json", "identity-candidate.blend", "asset-candidate.blend", "asset-change.json", "asset-index.json", "blender_assets.cats.txt", "scene-before.json", "scene-expected.json", "scene-report.json", "scene-actions.json", "scene-change.json", "scene-candidate.blend"):
            if (job / name).is_file():
                artifacts[name] = str(job / name)
        if tree:
            if tree.process.poll() is None:
                tree.terminate()
            worker_exit = tree.process.returncode
            if limits:usage.update(tree.usage())
            tree.close()
        if isolation:
            try:isolation.close()
            except Exception as ex:error=error or {'code':'IO_ERROR','message':'Isolated profile cleanup failed: '+str(ex)}
        if isolated_requested:
            for name in ('extension-report.json','isolation-report.json','isolation-state.json','isolation-cleanup.json',*request['params']['manifest']['outputs']):
                if (job/name).is_file():artifacts[name]=str(job/name)
        if lease:
            try:lease.release()
            except Exception as ex:
                error=error or {'code':'CONFLICT','message':'Lease retained after release failure: '+str(ex)}
        if limits:
            from telemetry import gpu_sample
            atomic_json(job/'telemetry.json',{'memory_samples':memory_observations,'gpu':gpu_sample(),'hard_memory':'Windows Job committed bytes','hard_disk':False,'hard_vram':False,'disk':'sampling and cancellation; no per-job filesystem quota'})
            atomic_json(job/'resources.json',{'limits':limits,'usage':usage,'memory_semantics':'Windows Job Object committed memory for bootstrap and all descendants','disk_semantics':'sampled budget; may overshoot, partial files retained','gpu_semantics':'scheduler reservation only, not a VRAM allocator limit'})
            artifacts['resources']=str(job/'resources.json')
        input_guards.close()
    code = CODES[error["code"]] if error else 0
    state.update(state=("cancelled" if code == 7 else "timed_out" if code == 6 else "failed" if code else "succeeded"),
                 exit_code=code, worker_exit_code=worker_exit, finished_at=time.time(), heartbeat_at=time.time(),
                 elapsed_seconds=round(time.monotonic() - started, 3), source_sha256_before=before)
    result = envelope(request["command"], job.name, data if not error else None, error, artifacts)
    atomic_json(job / "result.json", result)
    atomic_json(job / "status.json", state)
    return result, code


def execute(job, parent_checkpoint=None):
    # Host-specific leases may survive through final result/status publication.
    with ExitStack() as input_lifetime:
        return _execute(job, parent_checkpoint, input_lifetime)


def _execute(job, parent_checkpoint=None, input_lifetime=None):
    request = read_json(job / "request.json")
    if request['command'] in ('uv.batch','uv.batch-resume'):
        from uv_batch import execute as execute_uv_batch
        return execute_uv_batch(job,parent_checkpoint)
    if request['command'] in ('workflow.run', 'workflow.resume'):
        from workflow_runtime import execute as execute_workflow
        return execute_workflow(job, parent_checkpoint)
    if request['command'].startswith('pipeline.') or request['command']=='asset.preview.batch':
        import pipeline
        return pipeline.execute(job,parent_checkpoint)
    if request['command']=='modeling.run' and request['params']['manifest']['operation']=='import_local':
        return execute_worker(job,parent_checkpoint)
    if request["command"] not in HOST_COMMANDS:
        return execute_worker(job, parent_checkpoint)
    import transactions
    launch = read_json(job / "launch.json")
    state = read_json(job / "status.json")
    started = time.monotonic()
    state.update(state="running", supervisor_pid=os.getpid(), started_at=time.time())
    data, error = None, None
    image_recheck = None
    if request["command"]=="compositing.encode" or request['command'] in (*DEPTH_COMMANDS,*ESTIMATE_COMMANDS,*RELIGHT_COMMANDS,*SESSION_COMMANDS,*SEQUENCE_COMMANDS):
        state["process_tree_root_pid"]=os.getpid()  # Host supervisor roots its actual numeric/codec descendants.
    def checkpoint():
        if parent_checkpoint:
            parent_checkpoint()
        if (job / "cancel.request").exists():
            if request['command'] in ('toon-image.run','toon-image.inspect'):raise Failure('CANCELLED', 'Image job cancelled; retained partials cannot authorize output')
            if request['command'] in SEQUENCE_COMMANDS:raise Failure('CANCELLED','Sequence cancelled; partial frames cannot authorize a complete shot')
            if request['command'] in SESSION_COMMANDS:raise Failure('CANCELLED','Owned session cancelled; retained previews are diagnostic and no final closed receipt is authorized')
            if request['command'] in RELIGHT_COMMANDS:raise Failure('CANCELLED','Relight partial retained; cancelled parent cannot authorize output')
            if request['command'] in ESTIMATE_COMMANDS:raise Failure('CANCELLED','Estimate partial retained; cancelled parent cannot authorize raw or prepare recipe')
            if request['command'] in DEPTH_COMMANDS:raise Failure('CANCELLED','Depth partial retained; cancelled parent does not qualify a bundle')
            raise Failure("CANCELLED", "Encoder partial retained; only passed final output can resume" if request["command"]=="compositing.encode" else "Transaction job cancelled; inspect journal before recovery")
        if time.monotonic() - started > launch["timeout_seconds"]:
            if request['command'] in ('toon-image.run','toon-image.inspect'):raise Failure('TIMEOUT', 'Image parent deadline exceeded; retained partials cannot authorize output')
            if request['command'] in SEQUENCE_COMMANDS:raise Failure('TIMEOUT','Sequence timed out; partial frames remain diagnostic')
            if request['command'] in SESSION_COMMANDS:raise Failure('TIMEOUT','Owned session parent timeout; retained previews are diagnostic and no final closed receipt is authorized')
            if request['command'] in RELIGHT_COMMANDS:raise Failure('TIMEOUT','Relight partial retained; timed-out parent cannot authorize output')
            if request['command'] in ESTIMATE_COMMANDS:raise Failure('TIMEOUT','Estimate parent timeout; partial remains diagnostic only')
            if request['command'] in DEPTH_COMMANDS:raise Failure('TIMEOUT','Depth parent timeout; partial remains diagnostic only')
            raise Failure("TIMEOUT", "Encoder job timeout; partial evidence retained" if request["command"]=="compositing.encode" else "Transaction job exceeded timeout; journal retained")
        if time.time() - state["heartbeat_at"] >= 1:
            state["heartbeat_at"] = time.time()
            atomic_json(job / "status.json", state)
    atomic_json(job / "status.json", state)
    try:
        checkpoint()
        from protocol import OPERATIONS,REVIEWS
        if request['command']=='toon-image.run':
            from toon_image_host import run
            data, image_recheck = run(request, job, launch, input_lifetime, checkpoint)
            image_recheck()
        elif request['command']=='toon-image.inspect':
            from toon_image_host import inspect_run
            data, image_recheck = inspect_run(request, job, launch, input_lifetime, checkpoint)
            image_recheck()
        elif request['command']=='modeling.run':
            from modeling_host import run
            data=run(request['params'],job,checkpoint)
        elif request['command'] in SEQUENCE_COMMANDS:
            from compositing_relight_sequence_public import launch_identity,run
            from compositing_relight_sequence_contract import signature,DepthError
            if launch.get('sequence_identity')!=launch_identity() or launch.get('sequence_request_signature')!=signature(request['params']):
                raise Failure('CONFLICT','Sequence queued implementation/request changed')
            try:
                data=run(request['params'],job,cancel_parent=checkpoint,expected_identity=launch['sequence_identity'])
                checkpoint()
            except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED',str(exc)) from exc
        elif request['command'] in SESSION_COMMANDS:
            from compositing_relight_session_public import launch_identity,run
            from compositing_relight_session_contract import signature,DepthError
            if launch.get('session_identity')!=launch_identity() or launch.get('session_request_signature')!=signature(request['params']):
                raise Failure('CONFLICT','Session queued implementation/request changed')
            try:
                data=run(request['params'],job,checkpoint,expected_identity=launch['session_identity'])
                checkpoint()
                complete_path=job/'session-complete.json'
                if data.get('status')!='closed' or not complete_path.is_file():
                    raise Failure('VALIDATION_FAILED','Session success requires an explicitly closed complete receipt')
                receipt=read_json(complete_path);declared=data.get('complete',{})
                report_path=job/'session-report.json';report_ref=receipt.get('report',{})
                if (receipt.get('schema')!='relight-session-closed-v1' or receipt.get('status')!='complete'
                    or receipt.get('parent_job_id')!=job.name or declared.get('file')!=str(complete_path)
                    or declared.get('sha256')!=digest(complete_path) or not report_path.is_file()
                    or report_ref.get('file')!=str(report_path) or report_ref.get('sha256')!=digest(report_path)):
                    raise Failure('CONFLICT','Session closed receipt/report binding differs')
            except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED',str(exc)) from exc
        elif request['command'] in RELIGHT_COMMANDS:
            from compositing_relight_public import launch_identity,run
            from compositing_relight_contract import signature,DepthError
            if launch.get('relight_identity')!=launch_identity() or launch.get('relight_request_signature')!=signature(request['params']):
                raise Failure('CONFLICT','Relight queued implementation/request changed')
            try:
                data=run(request['params'],job,checkpoint,launch['relight_identity'])
                checkpoint()
            except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED',str(exc)) from exc
        elif request['command'] in ESTIMATE_COMMANDS:
            from compositing_depth_estimate_public import launch_identity,run
            from compositing_depth_estimate_contract import signature,DepthError
            if launch.get('estimate_identity') is None or launch['estimate_identity']!=launch_identity():
                raise Failure('CONFLICT','Estimate Host implementation differs from queued launch')
            if launch.get('estimate_request_signature')!=signature(request['params']):
                raise Failure('CONFLICT','Estimate queued request changed')
            try:
                data=run(request['params'],job,cancel_parent=checkpoint,expected_identity=launch['estimate_identity'])
                checkpoint()
            except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED',str(exc)) from exc
        elif request['command'] in DEPTH_COMMANDS:
            from compositing_depth_public import launch_identity,run
            from compositing_depth_contract import signature,DepthError
            if launch.get('depth_identity') is None or launch['depth_identity']!=launch_identity():
                raise Failure('CONFLICT','Depth Host implementation differs from queued launch')
            if launch.get('depth_request_signature')!=signature(request['params']):
                raise Failure('CONFLICT','Depth queued request changed')
            try:
                data=run(request['params'],job,request['command'].split('.')[1],cancel_parent=checkpoint,expected_identity=launch['depth_identity'])
                checkpoint()  # A returned bundle cannot bypass late parent cancel/timeout.
            except DepthError as exc:raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED',str(exc)) from exc
        elif request['command']=='compositing.encode':
            from compositing_encode_public import run_public
            data=run_public(request['params'],job,launch,checkpoint)
        elif request['command'] == 'workflow.preflight':
            from workflow_preflight import inspect_requests
            data = inspect_requests(request['params']['requests'], launch['blender'])
            atomic_json(job / 'workflow-preflight.json', data)
            if data['failed']:
                error = {'code': 'VALIDATION_FAILED', 'message': 'Full-scale static preflight failed; read workflow-preflight.json'}
        elif request['command'] in REVIEWS:
            import reviews
            data=reviews.run(request,launch,job,checkpoint)
        elif request['command'] in OPERATIONS:
            import operations
            data=operations.run(request,launch,job,checkpoint)
        else:data = transactions.run(request, launch, job, checkpoint)
    except KeyboardInterrupt:
        error = {"code": "CANCELLED", "message": "Interrupted; transaction journal retained"}
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    except Exception as exc:
        error = {"code": "WORKER_FAILED", "message": str(exc)}
        (job / "supervisor-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    code = CODES[error["code"]] if error else 0
    state.update(state="cancelled" if code == 7 else "timed_out" if code == 6 else "failed" if code else "succeeded",
                 exit_code=code, finished_at=time.time(), heartbeat_at=time.time(), elapsed_seconds=round(time.monotonic() - started, 3))
    artifacts = {"job_directory": str(job)}
    if request['command'] == 'toon-image.run':
        if (job / 'image-acceptance.json').is_file():
            artifacts['image_acceptance'] = str(job / 'image-acceptance.json')
        artifacts['image_run_directory'] = str(job / 'image-run')
    if request['command'] == 'toon-image.inspect':
        for name, key in (('parent-inspection.json', 'parent_inspection'),
                          ('parent-inspect-cold.json', 'parent_inspection_cold'),
                          ('image-inspect-context.json', 'inspector_context')):
            if (job / name).is_file():
                artifacts[key] = str(job / name)
    if request['command'] in SEQUENCE_COMMANDS:
        for name,key in [('sequence-report.json','sequence_report'),('sequence-bundle.json','sequence_bundle'),('sequence-failure.json','sequence_failure')]:
            if (job/name).is_file():artifacts[key]=str(job/name)
    if request['command'] in SESSION_COMMANDS:
        for name,key in [('session-ready.json','session_ready'),('session-report.json','session_report'),('session-complete.json','session_complete'),('session-failure.json','session_failure')]:
            if (job/name).is_file():artifacts[key]=str(job/name)
    if request['command'] in RELIGHT_COMMANDS:
        for name,key in [('relight-report.json','relight_report'),('relight-bundle.json','relight_bundle'),('relight-failure.json','relight_failure')]:
            if (job/name).is_file():artifacts[key]=str(job/name)
    if request['command'] in ESTIMATE_COMMANDS:
        for name,key in [('model-report.json','model_report'),('depth-prepare-params.json','estimate_prepare_params'),('model-lifecycle.json','estimate_lifecycle')]:
            if (job/name).is_file():artifacts[key]=str(job/name)
    if request['command'] in DEPTH_COMMANDS:
        for name,key in [('depth-report.json','depth_report')]:
            if (job/name).is_file():artifacts[key]=str(job/name)
        if request['command']=='compositing.depth-prepare' and (job/'depth-bundle.json').is_file():
            artifacts['depth_bundle']=str(job/'depth-bundle.json')
    if request['command'] == 'workflow.preflight' and (job / 'workflow-preflight.json').is_file():
        artifacts['preflight_report'] = str(job / 'workflow-preflight.json')
    if request["command"]=="compositing.encode" and (job/"encode-report.json").is_file():
        artifacts["encode_report"]=str(job/"encode-report.json")
        if (job/"encode-public-receipt.json").is_file():artifacts["encode_public_receipt"]=str(job/"encode-public-receipt.json")
        if (job/"encode-lifecycle.json").is_file():artifacts["encode_lifecycle"]=str(job/"encode-lifecycle.json")
    if request['command']=='modeling.run':
        for path in job.rglob('modeling-*'):
            if path.is_file():artifacts[str(path.relative_to(job))]=str(path)
    if data and "transaction_directory" in data:
        artifacts["transaction_directory"] = data["transaction_directory"]
    elif request["command"].startswith("transaction."):
        artifacts["transaction_directory"] = str(Path(launch["transactions_root"]) / request["params"]["transaction_id"])
    result = envelope(request["command"], job.name, data if not error else None, error, artifacts)
    atomic_json(job / "result.json", result)
    atomic_json(job / "status.json", state)
    return result, code


if __name__ == "__main__":
    _, exit_code = execute(Path(sys.argv[1]).resolve())
    raise SystemExit(exit_code)
