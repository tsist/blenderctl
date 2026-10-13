"""Main-thread owned-session queue. Strict JSON actions, persistent checkpoints."""
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

_root = None
_identity = None
_last = {'status': 'idle'}
_cli_runtime_dir = None


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''): digest.update(block)
    return digest.hexdigest()


def implementation_sha256():
    import sys
    import importlib
    runtime = Path(_cli_runtime_dir) if _cli_runtime_dir else Path(__file__).resolve().parents[1] / 'blenderctl'
    runtime = runtime.resolve()
    identity_path = runtime / 'geometry_identity.py'
    if not identity_path.is_file():
        raise ValueError('Select the installed project CLI runtime directory containing geometry_identity.py')
    installed = Path(__file__).resolve().parent
    source = runtime.parent / 'geometry_nodes_workflow_addon'
    actual_files = {p.name: _hash(p) for p in installed.glob('*.py')}
    source_files = {p.name: _hash(p) for p in source.glob('*.py')}
    if not source_files or actual_files != source_files:
        raise ValueError('Installed add-on and selected CLI add-on implementation differ; align versions before starting')
    if str(runtime) not in sys.path: sys.path.insert(0, str(runtime))
    identity = importlib.import_module('geometry_identity')
    if Path(identity.__file__).resolve() != identity_path:
        raise ValueError('Another CLI runtime is already loaded; reload in a fresh owned session')
    return identity.implementation_sha256()


def _write(path, value):
    temporary = Path(str(path) + '.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def _build():
    import bpy
    return {'version': list(bpy.app.version), 'hash': bpy.app.build_hash.decode()}


_BAKE_FIELDS = ('bake_target', 'bake_mode', 'frame_start', 'frame_end',
                'use_custom_simulation_frame_range', 'use_custom_path', 'directory')


def _cache_state(mod):
    """Guard disk cache metadata and content without resetting evaluation."""
    import bpy
    rows = []
    for item in mod.bakes:
        state = {key: getattr(item, key) for key in _BAKE_FIELDS}
        target = item.bake_target if item.bake_target != 'INHERIT' else mod.bake_target
        raw = item.directory if item.use_custom_path else mod.bake_directory
        state.update(bake_id=item.bake_id, effective_target=target)
        if raw and target == 'DISK':
            directory = Path(bpy.path.abspath(raw)).resolve()
            state['absolute_directory'] = str(directory)
            state['files'] = [{'path': p.relative_to(directory).as_posix(), 'sha256': _hash(p)}
                              for p in sorted(directory.rglob('*')) if p.is_file()]
        rows.append(state)
    return {'bake_target': mod.bake_target, 'bake_directory': mod.bake_directory, 'items': rows}


def _restore_cache_state(mod, state):
    mod.bake_target = state['bake_target']; mod.bake_directory = state['bake_directory']
    items = {item.bake_id: item for item in mod.bakes}
    if set(items) != {row['bake_id'] for row in state['items']}:
        raise ValueError('Restored graph bake identity differs')
    for row in state['items']:
        for key in _BAKE_FIELDS: setattr(items[row['bake_id']], key, row[key])


def baseline():
    """Conservative whole-scene guard, including user graph and geometry changes."""
    import bpy
    from .inspection import scene_fingerprint
    from .core import tree_fingerprint, plain, parameter_values, _parameter_state
    bpy.context.view_layer.update()
    objects = []
    for obj in sorted(bpy.data.objects, key=lambda x: x.name):
        mods = []
        for mod in obj.modifiers:
            props = {}
            for prop in mod.bl_rna.properties:
                if prop.identifier in ('rna_type', 'name', 'node_group') or prop.type == 'COLLECTION': continue
                try: props[prop.identifier] = plain(getattr(mod, prop.identifier))
                except (AttributeError, TypeError, RuntimeError): continue
            if mod.type == 'NODES' and mod.node_group:
                props['group'] = tree_fingerprint(mod.node_group)
                props['parameters'] = parameter_values(mod)
                props['parameter_state'] = _parameter_state(mod)
                props['cache'] = _cache_state(mod)
            mods.append([mod.name, mod.type, props])
        geometry = None
        if obj.type == 'MESH':
            geometry = {'vertices': [list(v.co) for v in obj.data.vertices], 'edges': [list(e.vertices) for e in obj.data.edges], 'faces': [list(p.vertices) for p in obj.data.polygons]}
        elif obj.type == 'CURVE':
            geometry = [[s.type, [list(p.co) for p in s.points], [[list(p.co), list(p.handle_left), list(p.handle_right)] for p in s.bezier_points], s.use_cyclic_u] for s in obj.data.splines]
        objects.append({'name': obj.name, 'type': obj.type, 'matrix': [list(r) for r in obj.matrix_world], 'data': obj.data.name if obj.data else None, 'geometry': geometry, 'modifiers': mods, 'collections': sorted(c.name for c in obj.users_collection), 'hide': [obj.hide_viewport, obj.hide_render]})
    groups = {}
    for group in bpy.data.node_groups:
        if group.bl_idname != 'GeometryNodeTree': continue
        layout = [{'name': n.name, 'location': list(n.location), 'width': n.width,
                   'parent': n.parent.name if n.parent else None, 'hide': n.hide,
                   'label': n.label, 'color': list(n.color), 'use_custom_color': n.use_custom_color}
                  for n in group.nodes]
        groups[group.name] = {'semantic': tree_fingerprint(group), 'layout': sorted(layout, key=lambda n: n['name'])}
    scene = bpy.context.scene
    state = {'scene': scene.name, 'view_layer': bpy.context.view_layer.name, 'frame': scene.frame_current,
             'content_sha256': scene_fingerprint(),
             'timeline': {'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
                          'start': scene.frame_start, 'end': scene.frame_end},
             'filepath': bpy.data.filepath, 'objects': objects, 'groups': groups}
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _descriptor(value):
    if not isinstance(value, dict) or set(value) != {'file', 'expected_sha256'}: raise ValueError('File/SHA descriptor required')
    path = Path(value['file'])
    if not path.is_absolute() or not path.is_file(): raise ValueError('Existing absolute file required')
    if _hash(path) != value['expected_sha256']: raise ValueError('Descriptor SHA conflict')
    return path


def _compare(expected, actual, path='evaluation', epsilon=1e-5):
    """Strict shape/finite-value comparison with explicit numeric tolerance."""
    if type(expected) in (int, float) and type(actual) in (int, float):
        if not math.isfinite(expected) or not math.isfinite(actual): raise ValueError(path + ': non-finite result')
        if not math.isclose(expected, actual, rel_tol=epsilon, abs_tol=epsilon): raise ValueError(path + ': numeric mismatch ' + str(expected) + ' != ' + str(actual))
    elif isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual): raise ValueError(path + ': keys differ')
        for key in expected: _compare(expected[key], actual[key], path + '.' + key, epsilon)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual): raise ValueError(path + ': array length differs')
        for index, (left, right) in enumerate(zip(expected, actual)): _compare(left, right, path + '[' + str(index) + ']', epsilon)
    elif type(expected) != type(actual) or expected != actual:
        raise ValueError(path + ': value differs')


def _observable(evaluation):
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get('evaluated'), dict): raise ValueError('Real evaluated inspection required')
    row = evaluation['evaluated']
    keys = ('vertices', 'faces', 'edges', 'bounds', 'instances', 'attributes', 'components', 'context')
    if any(key not in row for key in keys): raise ValueError('Incomplete evaluated inspection')
    return {key: row[key] for key in keys}


def _capture(request):
    import bpy
    from .contract import normalize_manifest
    manifest = normalize_manifest(request['manifest'])
    context = manifest['context']
    if context != {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name, 'frame': bpy.context.scene.frame_current}:
        raise ValueError('Manifest must identify the current scene, view layer and frame')
    before = baseline()
    path = _root / 'checkpoints' / (request['request_id'] + '.blend')
    if path.exists(): raise ValueError('Checkpoint already exists')
    result = bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, check_existing=False)
    if 'FINISHED' not in result or not path.is_file(): raise RuntimeError('Save Copy failed')
    if baseline() != before: raise RuntimeError('Save Copy altered guarded scene state')
    resources = request.get('resources', [])
    if not isinstance(resources, list): raise ValueError('resources must be a list')
    for descriptor in resources: _descriptor(descriptor)
    capture = {'status': 'captured', 'baseline': before, 'file': str(path), 'expected_sha256': _hash(path), 'manifest': manifest, 'resources': resources, 'build': _build(), 'implementation_sha256': implementation_sha256(), 'was_dirty': bpy.data.is_dirty}
    existing_temporal = []
    for target in manifest['targets']:
        obj = bpy.data.objects.get(target['object'])
        mod = obj.modifiers.get(target['modifier']) if obj else None
        if mod and mod.type == 'NODES' and mod.bakes:
            existing_temporal.append(target)
    if existing_temporal:
        if not manifest.get('temporal'):
            raise ValueError('Existing dynamic target capture requires temporal specification')
        from geometry_temporal import capture_manifest
        cache_job = path.with_suffix('.cache-capture')
        cache_job.mkdir()
        capture['cache_manifest'] = capture_manifest(manifest['temporal'], existing_temporal,
            manifest['observations'], cache_job, path, capture['implementation_sha256'])
        if baseline() != before:
            raise RuntimeError('Temporal capture changed guarded scene state')
    _write(path.with_suffix('.json'), capture)
    return capture


def _all_ids():
    """Exact ID inventory, including indirect linked prototype dependencies."""
    import bpy
    return {p.identifier: {item for item in getattr(bpy.data, p.identifier) if isinstance(item, bpy.types.ID)}
            for p in bpy.data.bl_rna.properties if p.type == 'COLLECTION'
            and p.identifier != 'rna_type'}


def _remove_new_ids(before, unused_only=False):
    import bpy
    # Remove users before their dependencies; repeated passes handle nested groups.
    order = ['objects', 'node_groups', 'collections']
    order += [k for k in before if k not in order and k != 'libraries']
    order += ['libraries']
    while True:
        removed = False
        for name in order:
            collection = getattr(bpy.data, name)
            for item in list(collection):
                if item in before[name] or (unused_only and item.users > int(item.use_fake_user)): continue
                bpy.data.batch_remove(ids=[item])
                removed = True
        if not removed: break


def _remap_candidate_tree(tree, prototype, before, candidate, new_object=None, prototype_object=None):
    """Use linked source identities, never suffix guesses or old GROUP reuse."""
    import bpy
    from .core import NODE_ID
    kinds = ((bpy.types.Object, 'objects'), (bpy.types.Collection, 'collections'),
             (bpy.types.Material, 'materials'), (bpy.types.Image, 'images'))
    visited = {}
    def identity(value):
        return next((key for cls, key in kinds if isinstance(value, cls)), None)
    def resolve(value):
        kind = identity(value)
        if not kind: raise ValueError('Unsupported candidate pointer identity')
        if not value.library: raise ValueError('Prototype pointer lacks source library')
        library = Path(bpy.path.abspath(value.library.filepath)).resolve()
        local = library == candidate.resolve()
        matches = [item for item in before[kind] if item.name == value.name
                   and ((item.library is None) if local else
                        (item.library is not None and Path(bpy.path.abspath(item.library.filepath)).resolve() == library))]
        if len(matches) != 1: raise ValueError('Candidate source pointer missing or ambiguous: ' + kind + '/' + value.name)
        return matches[0]
    def pointers(actual, source):
        for prop in source.bl_rna.properties:
            if prop.type != 'POINTER' or prop.identifier in ('rna_type', 'id_data', 'node_tree'): continue
            value = getattr(source, prop.identifier)
            if not identity(value): continue
            replacement = resolve(value)
            current = getattr(actual, prop.identifier)
            if current == replacement: continue
            if prop.is_readonly: raise ValueError('Read-only candidate dependency pointer: ' + prop.identifier)
            setattr(actual, prop.identifier, replacement)
    def indexed(items, key):
        result = {}
        for item in items:
            name = key(item)
            if name in result: raise ValueError('Ambiguous candidate structural identity: ' + str(name))
            result[name] = item
        return result
    def paired(actual, source, key):
        left, right = indexed(actual, key), indexed(source, key)
        if set(left) != set(right): raise ValueError('Candidate structure differs from linked prototype')
        return [(left[k], right[k]) for k in right]
    def visit(actual, source):
        if actual in before['node_groups'] or actual.library or actual.override_library:
            raise ValueError('Only newly appended local graphs can be remapped')
        pointer = actual.as_pointer()
        if pointer in visited:
            if visited[pointer] != source.as_pointer(): raise ValueError('Candidate group pairing conflict')
            return
        visited[pointer] = source.as_pointer()
        if actual.bl_idname != source.bl_idname: raise ValueError('Candidate group type differs')
        for node, proto in paired(actual.nodes, source.nodes, lambda n: (n.get(NODE_ID, n.name), n.name)):
            if node.bl_idname != proto.bl_idname: raise ValueError('Candidate node type differs')
            pointers(node, proto)
            for direction in ('inputs', 'outputs'):
                for socket, original in paired(getattr(node, direction), getattr(proto, direction), lambda s: (s.identifier, s.bl_idname)):
                    pointers(socket, original)
            nested, original = getattr(node, 'node_tree', None), getattr(proto, 'node_tree', None)
            if bool(nested) != bool(original): raise ValueError('Candidate nested group differs')
            if nested: visit(nested, original)
        actual_sockets = [s for s in actual.interface.items_tree if s.item_type == 'SOCKET']
        source_sockets = [s for s in source.interface.items_tree if s.item_type == 'SOCKET']
        for socket, original in paired(actual_sockets, source_sockets, lambda s: (s.identifier, s.in_out, s.socket_type)):
            pointers(socket, original)
        actual.update_tag()
    visit(tree, prototype)
    if new_object is not None:
        mesh = new_object.data
        if not isinstance(mesh, bpy.types.Mesh) or mesh in before['meshes'] or mesh.library:
            raise ValueError('Only the new target Mesh material slots can be remapped')
        original = prototype_object.data if prototype_object else None
        if not isinstance(original, bpy.types.Mesh) or len(mesh.materials) != len(original.materials):
            raise ValueError('Candidate Mesh material slots differ from prototype')
        for index, material in enumerate(original.materials):
            mesh.materials[index] = resolve(material) if material else None


def _apply(request):
    import bpy
    from .core import inspect_binding, tree_fingerprint, _core_node, _restore_values, parameter_values, _parameter_state, _restore_parameter_state, stage_fresh_modifier, commit_fresh_modifier, rollback_fresh_modifier
    candidate = _descriptor(request['candidate']); report_path = _descriptor(request['report'])
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('candidate_sha256') != _hash(candidate): raise ValueError('Report candidate identity mismatch')
    actual_build = _build()
    report_build = report.get('blender', {})
    if report.get('implementation_sha256') != implementation_sha256() or report_build.get('version_tuple') != actual_build['version'] or report_build.get('build_hash') != actual_build['hash']: raise ValueError('Build or implementation identity mismatch')
    if report.get('geometry_report_version') != '1.0' or report.get('operation') != 'run': raise ValueError('Versioned run report required')
    reopen = report.get('verification', {}).get('independent_reopen', {})
    if reopen.get('status') != 'passed': raise ValueError('Independent reopen verification required')
    independent = json.loads(_descriptor(reopen.get('report')).read_text(encoding='utf-8'))
    if independent.get('geometry_verification_version') != '1.0' or independent.get('status') != 'passed': raise ValueError('Independent verifier report did not pass')
    for key in ('candidate_sha256', 'implementation_sha256', 'blender'):
        if independent.get(key) != report.get(key): raise ValueError('Independent verifier identity mismatch: ' + key)
    if request['baseline'] != baseline(): raise ValueError('External scene change; candidate application stopped')
    temporal = report.get('temporal')
    cache = None
    if temporal:
        from geometry_temporal import validate_cache
        cache = validate_cache(temporal['cache_manifest'],
            candidate={'file': str(candidate), 'sha256': _hash(candidate)}, identity=implementation_sha256())
        if independent.get('temporal', {}).get('status') != 'passed':
            raise ValueError('Independent temporal replay required before session application')
    captures = [json.loads(p.read_text(encoding='utf-8')) for p in (_root / 'checkpoints').glob('*.json')]
    captured = next((c for c in captures if c['baseline'] == request['baseline'] and report.get('source') == {'file': c['file'], 'sha256': c['expected_sha256']} and report.get('manifest') == c['manifest']), None)
    if captured is None: raise ValueError('Run report does not match an owned capture and manifest')
    targets = [dict(t, **t.get('binding', {})) for t in report.get('targets', []) if t.get('status') == 'passed']
    if not targets: raise ValueError('No successfully validated target in report')
    verified = {t['id']: t for t in independent.get('targets', [])}
    if len(verified) != len(independent.get('targets', [])): raise ValueError('Duplicate verifier target ID')
    for target in targets:
        proof = verified.get(target['id'])
        if proof is None or proof.get('object') != target['object'] or proof.get('modifier') != target['modifier']: raise ValueError('Independent target identity mismatch')
        for key in ('core_sha256', 'wrapper_sha256', 'parameters', 'parameter_state'):
            _compare(proof.get('binding', {}).get(key), target[key], 'binding.' + key)
        _compare(_observable(proof.get('evaluation')), _observable(target.get('evaluation')))
    outcomes = []
    for target in targets:
        before_ids = _all_ids()
        obj = bpy.data.objects.get(target['object']); mod = obj.modifiers.get(target['modifier']) if obj else None
        oldgroup = mod.node_group if mod else None
        oldvalues = parameter_values(mod) if oldgroup else {}
        oldstate = _parameter_state(mod) if oldgroup else {}
        oldcache = _cache_state(mod) if oldgroup else None
        created = False; created_mod = False; imported = None; fresh_token = None
        try:
            if obj is not None and (obj.library or obj.override_library): raise ValueError('Linked/override target protected')
            if obj is None:
                with bpy.data.libraries.load(str(candidate), link=False) as (source, dest):
                    if target['object'] not in source.objects: raise ValueError('Candidate target missing')
                    dest.objects = [target['object']]
                obj = dest.objects[0]; imported = obj; created = True
                obj.name = target['object']; bpy.context.scene.collection.objects.link(obj)
                mod = obj.modifiers.get(target['modifier'])
            else:
                with bpy.data.libraries.load(str(candidate), link=False) as (source, dest):
                    if target['wrapper'] not in source.node_groups: raise ValueError('Candidate wrapper missing')
                    dest.node_groups = [target['wrapper']]
                wrapper = dest.node_groups[0]
                if mod is None: mod = obj.modifiers.new(target['modifier'], 'NODES'); created_mod = True
                if mod.type != 'NODES': raise ValueError('Target modifier type conflict')
                if cache and not created_mod:
                    fresh_token = stage_fresh_modifier(obj, mod, wrapper)
                    mod = fresh_token['fresh']
                else: mod.node_group = wrapper
            if mod is None or mod.type != 'NODES' or not mod.node_group: raise ValueError('Candidate modifier binding missing')
            prototype_before = _all_ids()
            try:
                with bpy.data.libraries.load(str(candidate), link=True) as (source, dest):
                    if target['wrapper'] not in source.node_groups: raise ValueError('Candidate prototype wrapper missing')
                    dest.node_groups = [target['wrapper']]
                    if created and obj.type == 'MESH' and len(obj.data.materials):
                        if target['object'] not in source.objects: raise ValueError('Candidate prototype object missing')
                        dest.objects = [target['object']]
                prototype_object = dest.objects[0] if created and obj.type == 'MESH' and len(obj.data.materials) else None
                _remap_candidate_tree(mod.node_group, dest.node_groups[0], before_ids, candidate,
                                      obj if prototype_object else None, prototype_object)
            finally:
                _remove_new_ids(prototype_before)
            if tree_fingerprint(_core_node(mod.node_group).node_tree) != target['core_sha256']: raise ValueError('Candidate core fingerprint mismatch')
            _restore_parameter_state(mod, target['parameter_state'])
            if cache:
                from geometry_temporal import configure_from_cache
                configure_from_cache(cache, target_map={target['id']: {'object': obj.name, 'modifier': mod.name}},
                                     before_first_evaluation=True)
            obj.update_tag()
            bpy.context.view_layer.update()
            binding = inspect_binding(obj.name, mod.name)
            if binding['core_sha256'] != target['core_sha256'] or binding['wrapper_sha256'] != target['wrapper_sha256'] or binding['parameters'] != target['parameters']: raise ValueError('Applied content differs from validated candidate')
            _compare(target['parameter_state'], binding['parameter_state'], 'binding.parameter_state')
            from .inspection import inspect_target
            evaluation = inspect_target(obj.name, mod.name)
            _compare(_observable(verified[target['id']]['evaluation']), _observable(evaluation))
            if cache:
                # The same imported binding must retain its state through
                # scrambled frames. Restore the user's capture frame afterward.
                oldframe = bpy.context.scene.frame_current
                try:
                    sample_by_frame = {s['frame']: s for s in cache['samples']}
                    for frame in cache['readback_order']:
                        bpy.context.scene.frame_set(frame)
                        expected = next(t['evaluation'] for t in sample_by_frame[frame]['targets'] if t['target'] == target['id'])
                        _compare(expected['evaluated'], inspect_target(obj.name, mod.name)['evaluated'], 'temporal.evaluated')
                finally:
                    bpy.context.scene.frame_set(oldframe)
            _remove_new_ids(before_ids, unused_only=True)
            if fresh_token: commit_fresh_modifier(fresh_token)
            outcomes.append({'id': target['id'], 'status': 'passed', 'binding': binding, 'evaluation': evaluation,
                             'temporal_replay': 'passed' if cache else 'not_applicable', 'comparison_epsilon': 1e-5})
        except Exception as exc:
            if created and imported: bpy.data.objects.remove(imported, do_unlink=True)
            elif mod:
                if fresh_token:
                    mod = rollback_fresh_modifier(fresh_token)
                elif created_mod: obj.modifiers.remove(mod)
                elif mod.node_group != oldgroup:
                    mod.node_group = oldgroup
                    if oldgroup: _restore_parameter_state(mod, oldstate)
                    if oldcache: _restore_cache_state(mod, oldcache)
                    obj.update_tag(); bpy.context.view_layer.update()
            _remove_new_ids(before_ids)
            outcomes.append({'id': target['id'], 'status': 'rolled_back', 'error': str(exc)})
    passed = sum(row['status'] == 'passed' for row in outcomes)
    outcome = 'passed' if passed == len(outcomes) else 'partial' if passed else 'failed'
    return {'status': 'applied', 'outcome': outcome, 'targets': outcomes, 'candidate': str(candidate), 'baseline_after': baseline()}


def _process(request):
    if implementation_sha256() != _identity['implementation_sha256']:
        raise ValueError('Implementation changed after queue start; reload runtime and start a fresh queue')
    if not isinstance(request, dict) or set(request) - {'request_id', 'session_id', 'action', 'manifest', 'resources', 'baseline', 'candidate', 'report'}: raise ValueError('Unknown request fields')
    if request.get('session_id') != _identity['id']: raise ValueError('Session identity mismatch')
    str(uuid.UUID(request['request_id']))
    action = request.get('action')
    if action == 'status': return dict(_last, build=_build(), baseline=baseline())
    if action == 'capture': return _capture(request)
    if action == 'apply': return _apply(request)
    raise ValueError('Unknown action')


def _pump():
    global _last
    if _root is None: return None
    for path in sorted((_root / 'requests').glob('*.json'))[:1]:
        result_path = _root / 'results' / path.name
        if result_path.exists():
            path.rename(path.with_suffix('.processed'))
            continue
        request_id = path.stem
        try:
            request = json.loads(path.read_text(encoding='utf-8'))
            if request.get('request_id') != request_id: raise ValueError('Request filename identity mismatch')
            value = _process(request); result = {'ok': True, 'request_id': request_id, 'session_id': _identity['id'], **value}
        except Exception as exc: result = {'ok': False, 'request_id': request_id, 'session_id': _identity['id'], 'status': 'failed', 'error': str(exc)}
        _last = result; _write(result_path, result)
        path.rename(path.with_suffix('.processed'))
    return .2


def start(session_dir, cli_runtime_dir=None):
    global _root, _identity, _cli_runtime_dir
    import bpy
    root = Path(session_dir)
    if not root.is_absolute(): raise ValueError('Absolute session directory required')
    if _root is not None: raise RuntimeError('Session queue already started')
    if (root / 'session.json').exists(): raise ValueError('Session directory already owned; choose a fresh directory')
    if cli_runtime_dir and not Path(cli_runtime_dir).is_absolute(): raise ValueError('Absolute CLI runtime directory required')
    _cli_runtime_dir = str(Path(cli_runtime_dir).resolve()) if cli_runtime_dir else None
    identity_sha = implementation_sha256()
    for name in ('requests', 'results', 'checkpoints'): (root / name).mkdir(parents=True, exist_ok=True)
    _identity = {'id': str(uuid.uuid4()), 'pid': os.getpid(), 'build': _build(), 'scene': bpy.context.scene.name,
                 'implementation_sha256': identity_sha, 'session_dir': str(root), 'cli_runtime_dir': _cli_runtime_dir}
    _write(root / 'session.json', _identity); _root = root
    bpy.app.timers.register(_pump, first_interval=.2, persistent=True)
    return dict(_identity)


def stop():
    global _root
    import bpy
    if bpy.app.timers.is_registered(_pump): bpy.app.timers.unregister(_pump)
    if _root is not None: _write(_root / 'stopped.json', {'id': _identity['id'], 'status': 'stopped'})
    _root = None


def status():
    return {'session': dict(_identity) if _root is not None else None, 'last': dict(_last)}
