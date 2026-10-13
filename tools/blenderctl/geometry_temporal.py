"""Bounded managed geometry temporal execution and immutable DISK cache receipts.

The caller owns scene persistence and must configure imported caches before its
first dependency-graph evaluation. This module never opens or saves a blend.
"""
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sys
import time
import traceback

# Rendering also imports this module without the geometry host contract.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VERSION = '1.0'
KINDS = {'SIMULATION': 'GeometryNodeSimulationOutput', 'BAKE': 'GeometryNodeBake'}
ITEM_FIELDS = ('bake_target', 'bake_mode', 'frame_start', 'frame_end',
               'use_custom_simulation_frame_range', 'use_custom_path', 'directory')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _within(root, relative):
    raw = Path(relative)
    if raw.is_absolute() or '..' in raw.parts:
        raise ValueError('Cache path must be relative without traversal')
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Cache path escapes cache root')
    return path


def _files(root):
    if not root.is_dir():
        raise ValueError('Missing cache directory: ' + str(root))
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Cache symlink or escaped member rejected')
        if path.is_file():
            result.append({'path': path.relative_to(root).as_posix(),
                           'size': path.stat().st_size, 'sha256': _sha(path)})
    return result


def _write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _runtime():
    import bpy
    return {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version),
            'build_hash': bpy.app.build_hash.decode('utf-8', errors='replace')}


def _resolve(target, node_id, kind, group_path=()):
    import bpy
    from geometry_nodes_workflow_addon import core
    obj = bpy.data.objects.get(target['object'])
    mod = obj.modifiers.get(target['modifier']) if obj else None
    if not mod or mod.type != 'NODES' or not mod.node_group:
        raise ValueError('Missing target NODES modifier')
    wrapper = mod.node_group
    if wrapper.get(core.ROLE) != 'wrapper':
        raise ValueError('Managed wrapper required for temporal target')
    managed = core._core_node(wrapper)
    tree = managed.node_tree if managed else None
    if tree is None:
        raise ValueError('Missing managed core')
    for selector in group_path:
        matches = [n for n in tree.nodes if n.get(core.NODE_ID) == selector]
        if len(matches) != 1 or matches[0].bl_idname != 'GeometryNodeGroup' or not matches[0].node_tree:
            raise ValueError('Missing or ambiguous stable group path: ' + selector)
        tree = matches[0].node_tree
    matches = [n for n in tree.nodes if n.get(core.NODE_ID) == node_id]
    if len(matches) != 1 or matches[0].bl_idname != KINDS[kind]:
        raise ValueError('Missing or ambiguous stable bake node: ' + node_id)
    node = matches[0]
    matched = [item for item in mod.bakes if item.node == node]
    # A repeated nested group can expose the same node through multiple bake IDs.
    if len(matched) != 1:
        raise ValueError('Bake node corresponds to zero or multiple modifier bake items')
    return obj, mod, node, matched[0]


def _metadata(mod, item):
    return {'global': {'bake_target': mod.bake_target, 'bake_directory': mod.bake_directory},
            'item': {field: getattr(item, field) for field in ITEM_FIELDS}}


def _binding(target):
    from geometry_nodes_workflow_addon import core
    value = core.inspect_binding(target['object'], target['modifier'])
    return {key: value.get(key) for key in ('wrapper_sha256', 'core_sha256', 'parameter_state', 'parameters')}


def _coverage(directory, start, end):
    frames = set()
    for path in (directory / 'meta').glob('*.json'):
        match = re.fullmatch(r'(-?\d+)_(\d+)\.json', path.name)
        if not match or int(match[2]) != 0:
            raise ValueError('Unknown metadata frame naming/subframe')
        frames.add(int(match[1]))
        content = json.loads(path.read_text(encoding='utf-8'))
        if content.get('version') != 3 or not isinstance(content.get('items'), dict):
            raise ValueError('Unknown metadata format')
        def references(value):
            if isinstance(value, dict):
                if isinstance(value.get('name'), str) and value['name'].endswith('.blob'):
                    blob = _within((directory / 'blobs').resolve(), value['name'])
                    if not blob.is_file(): raise ValueError('Referenced cache blob missing')
                    if value.get('start', 0) + value.get('size', 0) > blob.stat().st_size:
                        raise ValueError('Blob slice exceeds file size')
                for child in value.values(): references(child)
            elif isinstance(value, list):
                for child in value: references(child)
        references(content)
    if frames != set(range(start, end + 1)):
        raise ValueError('Cache metadata does not cover exactly the frame domain')
    return sorted(frames)


def _directory(job, request):
    token = hashlib.sha256(json.dumps([request['target'], request.get('group_path', []), request['node']],
                                     sort_keys=True).encode()).hexdigest()[:24]
    return job / CACHE_DIRECTORY / token


CACHE_DIRECTORY='tc'


def _path_budget(directory, start, end):
    # Current native cache format uses frame/subframe stems in meta and blobs.
    # Gate the concrete current-version member path before evaluating or baking.
    if os.name != 'nt':return
    from protocol import Failure
    stems=[('-' if frame<0 else '')+str(abs(frame)).zfill(5)+'_00000' for frame in (start,end)]
    paths=[directory/folder/(stem+suffix) for folder,suffix in (('meta','.json'),('blobs','.blob')) for stem in stems]
    if any(len(str(path))>=260 or len(str(path.parent))>=248 for path in paths):
        raise Failure('UNSUPPORTED','Native Bake output path exceeds Windows reader/writer budget; choose a shorter jobs-dir before production: '+str(max(paths,key=lambda p:len(str(p)))))


def _complete(modifier, selected):
    actual = list(modifier.bakes)
    actual_ids = [item.bake_id for item in actual]
    selected_ids = [item.bake_id for item in selected]
    if len(set(actual_ids)) != len(actual_ids) or len(set(selected_ids)) != len(selected_ids):
        raise ValueError('Ambiguous/duplicate bake IDs')
    if set(actual_ids) != set(selected_ids) or any(item.node is None for item in actual):
        raise ValueError('Temporal declaration must cover every actual modifier bake item')


def prepare_target(temporal, target, job, modifier=None):
    """Assign fresh job-owned DISK paths before the target's first evaluation."""
    from geometry_nodes_workflow_addon.contract import _validate, TEMPORAL_SCHEMA
    spec = _validate(_json(temporal), TEMPORAL_SCHEMA, 'temporal')
    job = Path(job).resolve()
    root = job / CACHE_DIRECTORY
    for request in spec['bakes']:
        if request['target']==target['id']:_path_budget(_directory(job,request),spec['frame_start'],spec['frame_end'])
    owner = root / 'owner.json'
    if not root.exists():
        root.mkdir()
        _write_new(owner, {'job': str(job), 'module_version': VERSION})
    if root.is_symlink() or root.resolve() != root:
        raise ValueError('Cache root symlink rejected')
    if not owner.is_file() or json.loads(owner.read_text(encoding='utf-8')) != {'job': str(job), 'module_version': VERSION}:
        raise ValueError('Existing cache root is not owned by this job')
    rows = []
    requests = [r for r in spec['bakes'] if r['target'] == target['id']]
    prepared = [_resolve(target, r['node'], r['kind'], r.get('group_path', [])) for r in requests]
    if prepared:
        _complete(prepared[0][1], [row[3] for row in prepared])
    for request in spec['bakes']:
        if request['target'] != target['id']: continue
        obj, mod, node, item = _resolve(target, request['node'], request['kind'], request.get('group_path', []))
        if modifier is not None and mod != modifier:
            raise ValueError('Preparation modifier differs from requested target')
        directory = _directory(job, request)
        if directory.exists() and _files(directory):
            raise ValueError('Prepared cache already contains files; new job required')
        directory.mkdir(exist_ok=True)
        mod.bake_target = 'DISK'; mod.bake_directory = str(root)
        item.bake_target = 'DISK'; item.bake_mode = 'ANIMATION'
        item.use_custom_path = True; item.directory = str(directory)
        item.use_custom_simulation_frame_range = True
        item.frame_start, item.frame_end = spec['frame_start'], spec['frame_end']
        rows.append({'node': request['node'], 'directory': str(directory), 'bake_id': item.bake_id})
    return {'status': 'prepared_before_evaluation', 'target': target['id'], 'bakes': rows}


def _samples(frame, targets, observations):
    import bpy
    from geometry_nodes_workflow_addon import inspection
    from geometry_verifier import stable_evaluation
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    rows = []
    for target in targets:
        selected = [o for o in observations if
                    (not o.get('object') or o['object'] == target['object']) and
                    (not o.get('modifier') or o['modifier'] == target['modifier'])]
        value = inspection.inspect_target(target['object'], target['modifier'], selected)
        rows.append({'target': target['id'], 'evaluation': stable_evaluation(value, True)})
    return _json({'frame': frame, 'targets': rows})


def _force_evaluation(frame, targets):
    """Advance actual GeometrySet evaluation without Python geometry inspection.

    Only unobserved frames use this path. Blender 5.2 evaluated_geometry is the
    same native GeometrySet entry point used by inspection._evaluated; no vertex,
    attribute, binding or graph fingerprint enumeration is needed here.
    """
    import bpy
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for target in targets:
        obj = bpy.data.objects.get(target['object'])
        if obj is None: raise ValueError('Target disappeared during temporal evaluation')
        obj.evaluated_get(depsgraph).evaluated_geometry()


def execute(temporal, targets, observations, job, progress_callback=None):
    """Create fresh caches, compare ordered and scrambled evidence, return draft.

    Failure leaves diagnostics and partial cache files for review, never promotes
    them to a manifest. Frame observations default to every frame in the budget.
    """
    import bpy
    from geometry_verifier import near
    from geometry_identity import implementation_sha256
    from geometry_nodes_workflow_addon.contract import _validate, TEMPORAL_SCHEMA
    from protocol import atomic_json
    started = time.monotonic()
    job = Path(job).resolve()
    report = {'temporal_report_version': VERSION, 'status': 'failed', 'stage': 'preflight',
              'metrics': {}, 'bakes': [], 'sequential': [], 'readback': []}
    phase_name = None; phase_started = None
    report['metrics']['phase_seconds'] = {}
    def finish_phase():
        nonlocal phase_name, phase_started
        if phase_name is not None:
            values = report['metrics']['phase_seconds']
            values[phase_name] = values.get(phase_name, 0.0) + time.monotonic() - phase_started
        phase_name = None; phase_started = None
    def begin_phase(name):
        nonlocal phase_name, phase_started
        finish_phase()
        phase_name = name; phase_started = time.monotonic()
    def progress(stage, **details):
        report['stage'] = stage
        if progress_callback:
            progress_callback({'stage': stage, **details})
    try:
        begin_phase('preflight')
        spec = _validate(_json(temporal), TEMPORAL_SCHEMA, 'temporal')
        start, end = spec['frame_start'], spec['frame_end']
        budget = spec.get('max_frames', 512)
        count = end - start + 1
        if count < 1 or count > budget or budget > 4096 or spec.get('storage', 'DISK') != 'DISK':
            raise ValueError('Temporal frame budget/storage rejected')
        frames = spec.get('observe_frames') or list(range(start, end + 1))
        if len(set(frames)) != len(frames) or any(f < start or f > end for f in frames):
            raise ValueError('Observation frame outside domain or duplicated')
        frames = sorted(frames)
        target_by_id = {t['id']: t for t in targets}
        if len(target_by_id) != len(targets):
            raise ValueError('Duplicate target ID')
        resolved, seen = [], set()
        for request in spec['bakes']:
            if request['target'] not in target_by_id:
                raise ValueError('Bake references missing target')
            obj, mod, node, item = _resolve(target_by_id[request['target']], request['node'],
                                            request['kind'], request.get('group_path', []))
            key = (obj.as_pointer(), mod.as_pointer(), item.bake_id)
            if key in seen:
                raise ValueError('Duplicate physical bake item')
            seen.add(key)
            resolved.append((request, obj, mod, node, item))
        cache_root = job / CACHE_DIRECTORY
        for target in targets:
            if any(r['target'] == target['id'] for r in spec['bakes']):
                prepare_target(spec, target, job)
        scene = bpy.context.scene
        fps = spec.get('fps', scene.render.fps / scene.render.fps_base)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Invalid fps')
        if not math.isclose(fps, scene.render.fps / scene.render.fps_base, rel_tol=1e-9):
            raise ValueError('Requested fps differs from actual scene fps; prepare scene explicitly')
        report.update(blender=_runtime(), implementation_sha256=implementation_sha256(),
                      fps=fps, frame_start=start, frame_end=end, observe_frames=frames,
                      targets=[{'id': t['id'], 'object': t['object'], 'modifier': t['modifier']} for t in targets])
        progress('configure')
        begin_phase('configure')
        for index, (request, obj, mod, node, item) in enumerate(resolved):
            directory = _directory(job, request)
            mod.bake_target = 'DISK'
            mod.bake_directory = str(cache_root)
            item.bake_target = 'DISK'; item.bake_mode = 'ANIMATION'
            item.use_custom_path = True; item.directory = str(directory)
            item.use_custom_simulation_frame_range = True
            item.frame_start, item.frame_end = start, end
            report['bakes'].append({'target': request['target'], 'object': obj.name, 'modifier': mod.name,
                'node_id': request['node'], 'group_path': request.get('group_path', []), 'kind': request['kind'],
                'node_type': node.bl_idname, 'bake_id': item.bake_id,
                'directory': directory.relative_to(job).as_posix(), 'metadata': _metadata(mod, item)})
        progress('sequential_evaluation', total=count)
        begin_phase('sequential_evaluation')
        observed = set(frames)
        report['metrics'].update(full_observation_frames=len(frames), native_force_frames=count-len(frames),
                                 timing_scope='phase_seconds are non-overlapping; elapsed_seconds is the parent and must not be added')
        for frame in range(start, end + 1):
            if frame in observed:
                report['sequential'].append(_samples(frame, targets, observations))
            else:
                _force_evaluation(frame, targets)
            if progress_callback:
                progress_callback({'stage': 'sequential_evaluation', 'frame': frame, 'total': count})
        progress('bake')
        # Simulation first; explicit Bake requests retain their declared order.
        order = sorted(range(len(resolved)), key=lambda i: resolved[i][0]['kind'] != 'SIMULATION')
        for index in order:
            request, obj, mod, node, item = resolved[index]
            begin_phase('bake_operators')
            # A saved source may have no active object. Bake the explicit target
            # through a temporary context without changing caller selection.
            with bpy.context.temp_override(object=obj, active_object=obj,
                    selected_objects=[obj], selected_editable_objects=[obj]):
                if not bpy.ops.object.geometry_node_bake_single.poll():
                    raise RuntimeError('Geometry node bake unavailable for explicit target context')
                result = bpy.ops.object.geometry_node_bake_single(session_uid=obj.session_uid,
                    modifier_name=mod.name, bake_id=item.bake_id)
            report['bakes'][index]['operator_result'] = sorted(result)
            if result != {'FINISHED'}:
                raise RuntimeError('Bake operator did not finish')
            begin_phase('cache_file_receipt')
            members = _files(job / report['bakes'][index]['directory'])
            if not members:
                raise ValueError('Finished bake has no persistent files')
            report['bakes'][index]['files'] = members
            report['bakes'][index]['frame_coverage'] = _coverage(job / report['bakes'][index]['directory'], start, end)
        progress('scrambled_readback')
        begin_phase('scrambled_readback')
        scrambled = list(reversed(frames))
        if len(scrambled) > 2:
            scrambled = scrambled[::2] + scrambled[1::2]
        reference = {row['frame']: row for row in report['sequential']}
        for frame in scrambled:
            sample = _samples(frame, targets, observations)
            mismatch = near(reference[frame], sample)
            report['readback'].append({**sample, 'status': 'failed' if mismatch else 'passed', 'mismatch': mismatch})
            if mismatch:
                raise ValueError('Baked scrambled readback differs at ' + mismatch)
        begin_phase('manifest_assembly')
        report['manifest_draft'] = {'cache_manifest_version': VERSION, 'root': str(job),
            'implementation_sha256': report['implementation_sha256'], 'blender': report['blender'],
            'fps': fps, 'frame_start': start, 'frame_end': end, 'observe_frames': frames,
            'scene_frame_range': [scene.frame_start, scene.frame_end],
            'targets': [{**t, 'binding': _binding(t)} for t in report['targets']], 'bakes': report['bakes'],
            'observations': observations, 'samples': report['sequential'], 'readback_order': scrambled,
            'scope': 'Sequential and scrambled same-process evidence; fresh reopen is caller-owned'}
        report['manifest'] = report['manifest_draft']
        report['manifest_output_root'] = str(job)
        report['status'] = 'passed'
        finish_phase()
        progress('awaiting_candidate_save')
        return report
    except Exception:
        report['traceback'] = traceback.format_exc()
        raise
    finally:
        finish_phase()
        report['metrics']['elapsed_seconds'] = time.monotonic() - started
        atomic_json(job / 'geometry-temporal-report.json', report)


def finalize_manifest(report, candidate, identity):
    """Bind the final saved candidate and create a write-once FILE receipt."""
    finalize_started = time.monotonic()
    if report.get('status') != 'passed' or not report.get('manifest_draft'):
        raise ValueError('Successful temporal draft required')
    manifest = copy.deepcopy(report['manifest_draft'])
    if identity != manifest['implementation_sha256']:
        raise ValueError('Implementation changed before manifest finalization')
    path = Path(candidate['file'] if isinstance(candidate, dict) else candidate).resolve()
    digest = _sha(path)
    if isinstance(candidate, dict) and candidate.get('sha256', digest) != digest:
        raise ValueError('Candidate SHA differs')
    manifest['candidate'] = {'file': str(path), 'sha256': digest}
    manifest['created_unix_seconds'] = time.time()
    root = Path(manifest['root']).resolve()
    for bake in manifest['bakes']:
        if _files(_within(root, bake['directory'])) != bake['files']:
            raise ValueError('Cache changed before finalization')
    manifest['files'] = sorted([{'path': (Path(bake['directory']) / member['path']).as_posix(),
        'size': member['size'], 'sha256': member['sha256']} for bake in manifest['bakes'] for member in bake['files']], key=lambda r: r['path'])
    output = Path(report.get('manifest_output_root', root)) / 'geometry-cache-manifest.json'
    _write_new(output, manifest)
    descriptor = {'file': str(output), 'expected_sha256': _sha(output)}
    report['cache_manifest'] = descriptor
    report.setdefault('metrics', {})['finalize_manifest_seconds'] = time.monotonic() - finalize_started
    return descriptor


def validate_cache(descriptor, candidate=None, identity=None):
    """Read-only SHA and complete cache-member-set validation."""
    path = Path(descriptor['file']).resolve()
    if _sha(path) != descriptor['expected_sha256']:
        raise ValueError('Cache manifest SHA mismatch')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('cache_manifest_version') != VERSION:
        raise ValueError('Unsupported cache manifest version')
    if identity is not None and manifest['implementation_sha256'] != identity:
        raise ValueError('Cache implementation identity mismatch')
    recorded = manifest['candidate']
    check = candidate or recorded
    if isinstance(check, str):
        check = {'file': check, 'sha256': recorded['sha256']}
    if check['sha256'] != recorded['sha256'] or _sha(check['file']) != recorded['sha256']:
        raise ValueError('Cache candidate SHA mismatch')
    root = Path(manifest['root']).resolve()
    directories = set()
    for bake in manifest['bakes']:
        directory = _within(root, bake['directory'])
        if directory in directories:
            raise ValueError('Duplicate cache directory')
        directories.add(directory)
        if _files(directory) != bake['files']:
            raise ValueError('Cache file set, size or SHA mismatch')
        _coverage(directory, manifest['frame_start'], manifest['frame_end'])
    if not directories:
        raise ValueError('Empty cache manifest')
    flattened = sorted([{'path': (Path(bake['directory']) / member['path']).as_posix(),
        'size': member['size'], 'sha256': member['sha256']} for bake in manifest['bakes'] for member in bake['files']], key=lambda r: r['path'])
    if flattened != manifest.get('files') or len({r['path'] for r in flattened}) != len(flattened):
        raise ValueError('Manifest flattened cache file inventory differs')
    return manifest


def configure_from_cache(manifest, target_map=None, *, before_first_evaluation=False):
    """Configure complete metadata without evaluation; explicit pre-eval promise.

    Loading metadata into a previously evaluated target is unverified and refused.
    Manifest must come from validate_cache; this method rechecks every cache SHA.
    """
    if before_first_evaluation is not True:
        raise ValueError('Cache configuration requires explicit before_first_evaluation=True')
    root = Path(manifest['root']).resolve()
    import bpy
    if _runtime()['build_hash'] != manifest['blender']['build_hash']:
        raise ValueError('Cache Blender build mismatch')
    bindings = target_map or {t['id']: t for t in manifest['targets']}
    expected_bindings = {t['id']: t['binding'] for t in manifest['targets']}
    if set(bindings) - set(expected_bindings):
        raise ValueError('Unknown cache target map ID')
    resolved = []
    globals_seen = {}
    for bake in manifest['bakes']:
        if bake['target'] not in bindings:
            if target_map is not None: continue
            raise ValueError('Cache target missing from map')
        directory = _within(root, bake['directory'])
        if _files(directory) != bake['files']:
            raise ValueError('Cache changed before configuration')
        obj, mod, node, item = _resolve(bindings[bake['target']], bake['node_id'], bake['kind'], bake['group_path'])
        if _binding(bindings[bake['target']]) != expected_bindings[bake['target']]:
            raise ValueError('Imported graph or parameter state differs from cache binding')
        if item.bake_id != bake['bake_id']:
            raise ValueError('Imported bake ID differs; mapping is not proven')
        global_metadata = bake['metadata']['global']
        key = mod.as_pointer()
        if key in globals_seen and globals_seen[key] != global_metadata:
            raise ValueError('Conflicting global modifier metadata')
        globals_seen[key] = global_metadata
        resolved.append((mod, item, bake, directory))
    # All validation precedes mutation; never update/evaluate here.
    if not resolved: raise ValueError('Cache target map resolves no bake items')
    for mod in {row[0].as_pointer(): row[0] for row in resolved}.values():
        _complete(mod, [row[1] for row in resolved if row[0] == mod])
    for mod, item, bake, directory in resolved:
        mod.bake_target = bake['metadata']['global']['bake_target']
        mod.bake_directory = bake['metadata']['global']['bake_directory']
        for field in ITEM_FIELDS:
            if field not in ('directory', 'use_custom_path'):
                setattr(item, field, bake['metadata']['item'][field])
        item.use_custom_path = True
        item.directory = str(directory)
    return {'status': 'configured_before_first_evaluation', 'bake_count': len(resolved)}


def capture_manifest(temporal, targets, observations, job, candidate, identity):
    """Read existing DISK cache evidence and bind a saved snapshot, without bake.

    Cache root is the common directory of all selected existing caches, while
    receipt/report files belong to this capture job. No cache file is copied.
    """
    import bpy
    from geometry_nodes_workflow_addon.contract import _validate, TEMPORAL_SCHEMA
    from geometry_identity import implementation_sha256
    from protocol import atomic_json
    started = time.monotonic()
    original_frame = bpy.context.scene.frame_current
    job = Path(job).resolve()
    report = {'status': 'failed', 'stage': 'capture_preflight', 'metrics': {}, 'bakes': []}
    try:
        spec = _validate(_json(temporal), TEMPORAL_SCHEMA, 'temporal')
        start, end = spec['frame_start'], spec['frame_end']
        if end < start or end - start + 1 > spec.get('max_frames', 512):
            raise ValueError('Capture frame domain exceeds budget')
        if implementation_sha256() != identity:
            raise ValueError('Capture implementation identity differs')
        actual_fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        if not math.isclose(spec.get('fps', actual_fps), actual_fps, rel_tol=1e-9):
            raise ValueError('Capture fps differs from live scene')
        target_by_id = {t['id']: t for t in targets}
        seen = set()
        directories = []
        capture_mods = {}
        for request in spec['bakes']:
            target = target_by_id[request['target']]
            obj, mod, node, item = _resolve(target, request['node'], request['kind'], request.get('group_path', []))
            physical = (obj.as_pointer(), mod.as_pointer(), item.bake_id)
            if physical in seen: raise ValueError('Duplicate capture bake item')
            seen.add(physical)
            capture_mods.setdefault(mod.as_pointer(), (mod, []))[1].append(item)
            target_storage = mod.bake_target if item.bake_target == 'INHERIT' else item.bake_target
            if target_storage != 'DISK' or item.bake_mode != 'ANIMATION':
                raise ValueError('Capture requires existing DISK animation caches')
            domain = (item.frame_start, item.frame_end) if item.use_custom_simulation_frame_range else (
                bpy.context.scene.frame_start, bpy.context.scene.frame_end)
            if domain != (start, end): raise ValueError('Capture bake frame metadata differs')
            raw = item.directory if item.use_custom_path else mod.bake_directory
            directory = Path(bpy.path.abspath(raw, library=obj.library)).resolve()
            members = _files(directory)
            if not members: raise ValueError('Capture cache is empty')
            coverage = _coverage(directory, start, end)
            directories.append(directory)
            metadata = _metadata(mod, item)
            metadata['global']['bake_directory'] = str(Path(bpy.path.abspath(mod.bake_directory)).resolve())
            report['bakes'].append({'target': request['target'], 'object': obj.name, 'modifier': mod.name,
                'node_id': request['node'], 'group_path': request.get('group_path', []), 'kind': request['kind'],
                'node_type': node.bl_idname, 'bake_id': item.bake_id, 'directory': str(directory),
                'metadata': metadata, 'files': members, 'frame_coverage': coverage})
        for mod, selected in capture_mods.values(): _complete(mod, selected)
        root = Path(os.path.commonpath([str(p) for p in directories])).resolve()
        for bake, directory in zip(report['bakes'], directories):
            bake['directory'] = directory.relative_to(root).as_posix()
        frames = spec.get('observe_frames') or list(range(start, end + 1))
        if len(set(frames)) != len(frames) or any(f < start or f > end for f in frames):
            raise ValueError('Capture observation frames invalid')
        scrambled = sorted(frames, reverse=True)
        if len(scrambled) > 2: scrambled = scrambled[::2] + scrambled[1::2]
        report['stage'] = 'capture_readback'
        samples = [_samples(frame, targets, observations) for frame in scrambled]
        for bake, directory in zip(report['bakes'], directories):
            if _files(directory) != bake['files']:
                raise ValueError('Cache changed during read-only capture')
        report['manifest_draft'] = {'cache_manifest_version': VERSION, 'root': str(root),
            'implementation_sha256': identity, 'blender': _runtime(), 'fps': actual_fps,
            'frame_start': start, 'frame_end': end, 'observe_frames': sorted(frames),
            'scene_frame_range': [bpy.context.scene.frame_start, bpy.context.scene.frame_end],
            'targets': [{**{k: t[k] for k in ('id', 'object', 'modifier')}, 'binding': _binding(t)} for t in targets],
            'bakes': report['bakes'], 'observations': observations, 'samples': samples,
            'readback_order': scrambled, 'scope': 'Existing DISK metadata/frame coverage/read-only samples; no rebake'}
        report['manifest'] = report['manifest_draft']
        report['manifest_output_root'] = str(job)
        report['status'] = 'passed'
        report['cache_manifest'] = finalize_manifest(report, candidate, identity)
        report['stage'] = 'complete'
        return report['cache_manifest']
    except Exception:
        report['traceback'] = traceback.format_exc()
        raise
    finally:
        bpy.context.scene.frame_set(original_frame)
        bpy.context.view_layer.update()
        report['metrics']['elapsed_seconds'] = time.monotonic() - started
        atomic_json(job / 'geometry-temporal-capture-report.json', report)


def validate_loaded_bindings(manifest, target_map=None):
    """Read-only cache identity checks in an already loaded candidate, no eval."""
    import bpy
    if _runtime()['build_hash'] != manifest['blender']['build_hash']:
        raise ValueError('Loaded Blender build differs from cache')
    scene = bpy.context.scene
    if [scene.frame_start, scene.frame_end] != manifest.get('scene_frame_range', [manifest['frame_start'], manifest['frame_end']]):
        raise ValueError('Loaded frame domain differs from cache')
    if not math.isclose(scene.render.fps / scene.render.fps_base, manifest['fps'], rel_tol=1e-9):
        raise ValueError('Loaded fps differs from cache')
    targets = {t['id']: t for t in manifest['targets']}
    bindings = target_map if target_map is not None else targets
    if set(bindings) - set(targets): raise ValueError('Unknown loaded cache target ID')
    root = Path(manifest['root']).resolve()
    count = 0
    selected_by_mod = {}
    for bake in manifest['bakes']:
        if bake['target'] not in bindings: continue
        target = bindings[bake['target']]
        if _binding(target) != targets[bake['target']]['binding']:
            raise ValueError('Loaded graph or parameter binding differs')
        obj, mod, node, item = _resolve(target, bake['node_id'], bake['kind'], bake['group_path'])
        selected_by_mod.setdefault(mod.as_pointer(), (mod, []))[1].append(item)
        if item.bake_id != bake['bake_id'] or node.bl_idname != bake['node_type']:
            raise ValueError('Loaded bake identity differs')
        expected = bake['metadata']['item']
        actual = _metadata(mod, item)
        for field in ITEM_FIELDS:
            if field != 'directory' and actual['item'][field] != expected[field]:
                raise ValueError('Loaded item metadata differs: ' + field)
        raw = item.directory if item.use_custom_path else mod.bake_directory
        if Path(bpy.path.abspath(raw, library=obj.library)).resolve() != _within(root, bake['directory']):
            raise ValueError('Loaded cache directory differs')
        if mod.bake_target != bake['metadata']['global']['bake_target']:
            raise ValueError('Loaded global bake_target differs')
        actual_global = Path(bpy.path.abspath(mod.bake_directory, library=obj.library)).resolve()
        expected_global = Path(bpy.path.abspath(bake['metadata']['global']['bake_directory'])).resolve()
        if actual_global != expected_global:
            raise ValueError('Loaded global cache directory differs')
        count += 1
    if not count: raise ValueError('Loaded bindings resolve no cache item')
    for mod, selected in selected_by_mod.values(): _complete(mod, selected)
    return {'status': 'passed', 'bake_count': count}
