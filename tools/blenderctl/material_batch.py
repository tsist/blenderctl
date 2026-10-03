# SPDX-License-Identifier: GPL-3.0-or-later
"""Protected single-worker material batches with immutable recovery checkpoints."""
import copy
import hashlib
import json
import os
import re
from contextlib import ExitStack
from pathlib import Path
import shutil

import bpy
from protocol import Failure, atomic_json, digest
from filesystem import FileGuard
from inspection import all_ids, identity
from material_batch_contract import normalize_params
from material_workflow_addon import batch, batch_preview, core, preview
from material_workflow_addon.batch_contract import topological_order
from material_workflow_addon.batch_contract import normalize_manifest as normalize_batch_manifest
from material_workflow_addon.contract import image_documents
import material_workflow as single
import rendering
import scenes
from material_progress import Progress


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def implementation():
    root = Path(__file__).resolve().parent
    paths = [root / name for name in ('material_batch.py', 'material_progress.py', 'material_workflow.py', 'material_batch_contract.py',
        'rendering.py', 'render_contract.py', 'scenes.py', 'nodes.py', 'inspection.py', 'protocol.py')]
    paths += [root.parent / 'material_workflow_addon' / name for name in (
        'batch.py', 'batch_contract.py', 'batch_preview.py', 'core.py', 'core_v2.py', 'contract.py', 'preview.py', 'engine_compat.py')]
    return fingerprint({'files': {p.name: digest(p) for p in paths},
                        'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode()})


def scope_fingerprint(params):
    m = params['manifest']
    return fingerprint({'source': params['expected_sha256'], 'context': m['context'], 'objects': sorted(m['objects']),
        'assignments': sorted([{k: a[k] for k in ('id', 'target', 'material', 'faces')} for a in m['assignments']], key=lambda a: a['id'])})


def task_fingerprints(manifest, impl, source_sha):
    specs = {a['id']: a for a in manifest['assignments']}
    result = {}
    for task_id in topological_order(manifest):
        spec = specs[task_id]
        result[task_id] = task_fingerprint(spec, manifest, impl, source_sha, {d: result[d] for d in spec['depends_on']})
    return result


def semantic_layers(layers):
    result = copy.deepcopy(layers)
    for layer in result:
        for image in [*layer['channels'].values(), *([layer['mask']] if layer['mask'] else [])]: image.pop('file', None)
    return result


def task_fingerprint(assignment, manifest, impl, source_sha, dependencies):
    spec = copy.deepcopy(assignment); spec['layers'] = semantic_layers(spec['layers'])
    return fingerprint({'implementation': impl, 'source': source_sha, 'context': manifest['context'],
                        'assignment': spec, 'dependencies': dependencies,
                        'engine': manifest['preview']['engine']})


def view_fingerprint(view, manifest, impl, source_sha, tasks):
    deps = [a['id'] for a in manifest['assignments'] if a['target']['object'] in view['objects']]
    return fingerprint({'implementation': impl, 'source': source_sha, 'context': manifest['context'],
        'preview': {k: v for k, v in manifest['preview'].items() if k != 'views'}, 'view': view,
        'assignments': {d: tasks[d] for d in deps}})


def _err(error):
    converted = single.failure(error)
    return {'code': converted.code, 'message': str(converted)}


def _open(path, context):
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED', 'Batch requires matching Blender major/minor')
    single.activate(context)


def _check_static_source():
    if bpy.data.sounds or bpy.data.movieclips or bpy.data.cache_files or bpy.data.volumes:
        raise Failure('UNSUPPORTED', 'Batch source media/cache/volume dependencies require another adapter')


def _guard(doc, stack):
    guard = stack.enter_context(FileGuard(doc['file']))
    if guard.sha256() != doc['expected_sha256']:
        raise Failure('CONFLICT', 'Input SHA differs: ' + doc['file'])
    return guard


def _artifact(doc, root):
    if not isinstance(doc, dict) or not isinstance(doc.get('file'), str) or not isinstance(doc.get('expected_sha256'), str):
        raise Failure('INVALID_REQUEST', 'Resume artifact requires file and SHA')
    path = Path(doc['file']).resolve()
    if not path.is_relative_to(root) or path == root or not re.fullmatch('[0-9a-f]{64}', doc['expected_sha256']):
        raise Failure('INVALID_REQUEST', 'Resume artifact lies outside its owning job')
    return {'file': str(path), 'expected_sha256': doc['expected_sha256']}


def load_resume(params, report, stack):
    if 'resume' not in params: return None
    descriptor = params['resume']; _guard(descriptor, stack)
    root = Path(descriptor['file']).resolve().parent
    prior = json.loads(Path(descriptor['file']).read_text(encoding='utf-8'))
    if prior.get('operation') != 'material.batch' or prior.get('report_version') != '1.0':
        raise Failure('INVALID_REQUEST', 'Not a material batch recovery report')
    prior['manifest'] = normalize_batch_manifest(prior['manifest'])
    old_params = {**prior['source'], 'manifest': prior['manifest']}
    if scope_fingerprint(old_params) != prior.get('scope_fingerprint'):
        raise Failure('CONFLICT', 'Recovery scope does not match recorded inputs')
    if prior.get('implementation') != report['implementation']:
        report['resume']['reason'] = 'implementation_changed_full_rebuild'; return None
    if prior.get('scope_fingerprint') != report['scope_fingerprint']:
        report['resume']['reason'] = 'source_or_assignment_scope_changed_full_rebuild'; return None
    if prior['manifest']['preview']['engine'] != params['manifest']['preview']['engine']:
        # A failed engine-specific assignment must not publish an old-engine
        # checkpoint record under the new manifest's compatibility contract.
        report['resume']['reason'] = 'engine_changed_full_rebuild'; return None
    cp = prior.get('checkpoint')
    if not cp:
        report['resume']['reason'] = 'no_verified_checkpoint_full_rebuild'; return None
    _guard(_artifact(cp, root), stack)
    _guard(_artifact(cp['snapshot'], root), stack)
    if not isinstance(cp['resources'], list) or len(cp['resources']) > 8192:
        raise Failure('INVALID_REQUEST', 'Unbounded checkpoint resources')
    for resource in cp['resources']: _guard(_artifact(resource, root), stack)
    if not isinstance(cp.get('assignments'), list) or len(cp['assignments']) > 64:
        raise Failure('INVALID_REQUEST', 'Invalid checkpoint assignment records')
    old_ids = [a['id'] for a in cp['assignments']]
    allowed = {a['id'] for a in params['manifest']['assignments']}
    if len(set(old_ids)) != len(old_ids) or not set(old_ids) <= allowed:
        raise Failure('INVALID_REQUEST', 'Checkpoint assignment identity differs')
    expected_scopes = {a['id']: {k: a[k] for k in ('id', 'target', 'material', 'faces')} for a in prior['manifest']['assignments']}
    for row in cp['assignments']:
        request = row.get('request', {})
        deps = row.get('dependency_fingerprints', {})
        if (row.get('status') != 'pass' or row.get('applied') is not True or type(row.get('reused')) is not bool
                or not isinstance(row.get('fingerprint'), str) or not re.fullmatch('[0-9a-f]{64}', row['fingerprint'])
                or {k: request.get(k) for k in ('id', 'target', 'material', 'faces')} != expected_scopes[row['id']]
                or not isinstance(deps, dict) or set(deps) != set(request.get('depends_on', []))):
            raise Failure('INVALID_REQUEST', 'Checkpoint must contain valid successful applied assignment records')
        if task_fingerprint(request, prior['manifest'], prior['implementation'], prior['source']['expected_sha256'], deps) != row['fingerprint']:
            raise Failure('CONFLICT', 'Checkpoint task fingerprint differs from its recorded request')
    outputs = prior.get('outputs', [])
    if not isinstance(outputs, list) or len(outputs) > 24 or len({o['id'] for o in outputs}) != len(outputs):
        raise Failure('INVALID_REQUEST', 'Invalid recovery view records')
    old_views = {v['id']: v for v in prior['manifest']['preview']['views']}
    old_tasks = task_fingerprints(prior['manifest'], prior['implementation'], prior['source']['expected_sha256'])
    for output in outputs:
        if output['id'] not in old_views or output.get('fingerprint') != view_fingerprint(old_views[output['id']], prior['manifest'], prior['implementation'], prior['source']['expected_sha256'], old_tasks):
            raise Failure('CONFLICT', 'Recovery view fingerprint differs from recorded inputs')
    _open(cp['file'], params['manifest']['context'])
    observed = single.snapshot()
    mismatch = scenes.compare(json.loads(Path(cp['snapshot']['file']).read_text(encoding='utf-8')), observed)
    if mismatch: raise Failure('VALIDATION_FAILED', 'Checkpoint content differs: ' + mismatch)
    for row in cp['assignments']:
        request = row['request']; matches = [m for m in bpy.data.materials if m.get(core.KEY) == request['material']['id']]
        if len(matches) != 1 or 'mw_state_v2' not in matches[0]:
            raise Failure('CONFLICT', 'Checkpoint material baseline missing')
        stored = core.get_stored_manifest(matches[0])
        if (any(stored[k] != request[k] for k in ('target', 'material')) or stored['context'] != prior['manifest']['context']
                or semantic_layers(stored['layers']) != semantic_layers(request['layers'])):
            raise Failure('CONFLICT', 'Checkpoint material recipe differs from recorded assignment')
        obj = scenes.find(bpy.data.objects, request['target']['object'])
        slot = request['target']['material_slot']
        if slot >= len(obj.material_slots) or obj.material_slots[slot].material != matches[0] or obj.material_slots[slot].link != 'OBJECT':
            raise Failure('CONFLICT', 'Checkpoint assignment binding differs')
        if any(i >= len(obj.data.polygons) or obj.data.polygons[i].material_index != slot for i in request['faces'] or []):
            raise Failure('CONFLICT', 'Checkpoint face assignment differs')
    report['resume'].update(loaded=True, reason='verified_checkpoint')
    prior['_root'] = root
    return prior


def _verify_assignment(before, after, obj, old_faces, assignment, manifest, before_images, applied):
    expected = list(old_faces)
    for index in assignment['faces'] or []: expected[index] = assignment['target']['material_slot']
    if [p.material_index for p in obj.data.polygons] != expected:
        raise Failure('VALIDATION_FAILED', 'Assignment changed undeclared face material indices')
    a, b = copy.deepcopy(before), copy.deepcopy(after)
    # Independent exact face validation above covers this one mesh field.
    if assignment['faces'] is not None:
        for state in (a, b):
            for row in state['scene']['datablocks']:
                if row['type'] == 'Mesh' and row['name'] == obj.data.name:
                    row['mesh']['hashes'].pop('material_index', None)
                    row['mesh']['attributes'] = [x for x in row['mesh']['attributes'] if x['name'] not in ('material_index', '.material_index')]
                    row['mesh']['diagnostics'].pop('invalid_material_indices', None)
    # Empty intermediary slots are the only additional binding allowance.
    for row in a['bindings']:
        if row['object'] == obj.name:
            while len(row['slots']) < assignment['target']['material_slot']:
                row['slots'].append({'material': None, 'link': 'DATA'})
    images = single.new_target_images(bpy.data.materials[applied['material_name']], before_images, manifest)
    single.verify_preservation(a, b, manifest, obj.data.name, images)


def run(params, job, observer=None):
    progress = Progress(observer); job = Path(job).resolve()
    report = {'report_version': '1.0', 'operation': 'material.batch', 'status': 'running',
        'report': str(job / 'batch-report.json'), 'source_saved': False, 'assignments': [], 'outputs': [],
        'checkpoint': None, 'resources': [], 'sheet': None, 'checks': {},
        'resume': {'requested': 'resume' in params, 'loaded': False, 'reason': 'not_requested', 'assignment_reuse': 0, 'view_reuse': 0}}
    def publish(stage):
        report['timings'] = progress.timings()
        report['seconds'] = report['timings']['elapsed_seconds']
        atomic_json(job / 'batch-report.json', report)
        document = progress.document(report, stage)
        atomic_json(job / 'batch-progress.json', document)
        progress.notify(document)
    try:
        params = normalize_params(params); manifest = params['manifest']
        report.update(source={k: params[k] for k in ('file', 'expected_sha256')}, manifest=manifest,
                      implementation=implementation(), scope_fingerprint=scope_fingerprint(params))
        task_keys = task_fingerprints(manifest, report['implementation'], params['expected_sha256'])
        by_id = {a['id']: a for a in manifest['assignments']}
        atomic_json(job / 'manifest.json', manifest)
        publish('preflight')
        with ExitStack() as guards:
            _guard(report['source'], guards)
            # Original saved scene is a shared dependency even when resuming.
            _open(params['file'], manifest['context']); _check_static_source()
            spec = single.render_spec(manifest, manifest['context']['scene'], manifest['context']['view_layer'], 'preflight')
            rendering.preflight(params, job, spec, guards)
            progress.switch('resume')
            prior = load_resume(params, report, guards)
            progress.switch('preflight_resources')
            applied_state = {a['id']: a for a in prior['checkpoint']['assignments']} if prior else {}
            source_resources = list(params['resources']) + (prior['checkpoint']['resources'] if prior else [])
            mapping, staged, resource_errors, resource_guards = {}, {}, {}, {}
            folder = job / 'textures'; folder.mkdir()
            for resource in source_resources:
                path = str(Path(resource['file']).resolve()); key = os.path.normcase(path)
                if key in mapping or key in resource_errors: continue
                try:
                    guard = _guard(resource, guards); resource_guards[path] = (guard, resource['expected_sha256'])
                    dest = folder / (resource['expected_sha256'] + Path(path).suffix.lower())
                    if not dest.exists(): shutil.copyfile(path, dest)
                    if digest(dest) != resource['expected_sha256']: raise Failure('VALIDATION_FAILED', 'Staged copy differs')
                    mapping[key] = str(dest); staged[str(dest)] = resource['expected_sha256']
                except Failure as error:
                    resource_errors[key] = _err(error)
            report['resource_errors'] = resource_errors
            resources = [{'file': path, 'expected_sha256': sha} for path, sha in staged.items()]
            report['resources'] = resources
            atomic_json(job / 'resources.json', resources)
            # Rebase only static content into this new job. No source .blend is saved.
            for resource in resources: mapping[os.path.normcase(resource['file'])] = resource['file']
            single.localize_existing_resources(mapping)
            retained = []
            report['retained_orphans'] = retained
            publish('resources_ready')
            sequence = 0
            def checkpoint():
                nonlocal sequence
                previous_phase = progress.phase
                progress.switch('checkpoint_save')
                dest = job / ('candidate-%04d.blend' % sequence)
                snapshot_path = job / ('candidate-%04d.snapshot.json' % sequence)
                sequence += 1
                if dest.exists(): raise Failure('CONFLICT', 'Checkpoint destination exists')
                single.activate(manifest['context'])
                # Replacing a texture may orphan an old datablock after startup.
                # Preserve it before each save; never let reopening silently purge it.
                for item in all_ids():
                    if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
                        item.use_fake_user = True; retained.append(identity(item))
                bpy.context.preferences.filepaths.save_version = 0
                bpy.ops.wm.save_as_mainfile(filepath=str(dest), copy=False, relative_remap=True, check_existing=False)
                single.relative_resources(job)
                expected = single.snapshot()
                atomic_json(snapshot_path, expected)
                bpy.ops.wm.save_as_mainfile(filepath=str(dest), copy=False, relative_remap=True, check_existing=False)
                progress.switch('checkpoint_reopen')
                _open(dest, manifest['context'])
                mismatch = scenes.compare(expected, single.snapshot())
                if mismatch: raise Failure('VALIDATION_FAILED', 'Checkpoint reopen differs: ' + mismatch)
                for image in bpy.data.images:
                    if image.source == 'FILE' and not image.packed_file:
                        if not image.filepath.startswith('//') or not Path(bpy.path.abspath(image.filepath)).resolve().is_relative_to(job):
                            raise Failure('VALIDATION_FAILED', 'Checkpoint dependency not packaged relative to job')
                report['checkpoint'] = {'file': str(dest), 'expected_sha256': digest(dest),
                    'snapshot': {'file': str(snapshot_path), 'expected_sha256': digest(snapshot_path)},
                    'resources': resources, 'assignments': list(applied_state.values())}
                report['candidate'] = str(dest); report['candidate_sha256'] = report['checkpoint']['expected_sha256']
                report['checks']['checkpoint_reopen'] = 'pass'
                publish('checkpoint')
                progress.switch(previous_phase)
            checkpoint()
            rows = {}
            for task_id in topological_order(manifest):
                progress.switch('assembly'); progress.assignment = task_id
                assignment = by_id[task_id]
                row = {'id': task_id, 'status': 'failed', 'fingerprint': task_keys[task_id], 'reused': False, 'applied': False}
                row.update(request=copy.deepcopy(assignment), dependency_fingerprints={d: task_keys[d] for d in assignment['depends_on']})
                publish('assignment:' + task_id)
                dependencies = [d for d in assignment['depends_on'] if rows[d]['status'] != 'pass']
                task_manifest = batch.assignment_manifest(manifest, assignment)
                errors = [resource_errors[os.path.normcase(i['file'])] for i in image_documents(task_manifest) if os.path.normcase(i['file']) in resource_errors]
                if dependencies or errors:
                    row.update(status='blocked', error={'code': 'DEPENDENCY_FAILED', 'message': 'Assignments: '+str(dependencies) if dependencies else errors[0]['message']}, resource_errors=errors)
                elif task_id in applied_state and applied_state[task_id]['fingerprint'] == task_keys[task_id]:
                    row.update(status='pass', reused=True, applied=True, result=applied_state[task_id].get('result'))
                    report['resume']['assignment_reuse'] += 1
                else:
                    before = single.snapshot(); before_images = set(bpy.data.images)
                    try:
                        obj = scenes.find(bpy.data.objects, assignment['target']['object'])
                        old_faces = [p.material_index for p in obj.data.polygons] if obj.type == 'MESH' else []
                        existing = [m for m in bpy.data.materials if m.get(core.KEY) == assignment['material']['id']]
                        if any(core.STATE in m for m in existing):
                            raise Failure('UNSUPPORTED', 'Upgrade existing v1 material with material run before batching')
                        transport = {i['file']: mapping[os.path.normcase(i['file'])] for i in image_documents(task_manifest)}
                        applied = batch.apply_assignment(manifest, assignment, transport)
                    except Exception as error:
                        if getattr(error, 'code', '') == 'batch_rollback_failed': raise Failure('VALIDATION_FAILED', str(error)) from error
                        mismatch = scenes.compare(before, single.snapshot())
                        if mismatch: raise Failure('VALIDATION_FAILED', 'Failed assignment left changes: ' + mismatch) from error
                        row['error'] = _err(error)
                    else:
                        # Failed preservation is a fatal contract breach, never an isolated success.
                        _verify_assignment(before, single.snapshot(), obj, old_faces, assignment, task_manifest, before_images, applied)
                        row.update(status='pass', applied=True, result=applied)
                        applied_state[task_id] = copy.deepcopy(row)
                        report['assignments'].append(row); rows[task_id] = row
                        checkpoint()
                        continue
                report['assignments'].append(row); rows[task_id] = row
                publish('assignment_complete:' + task_id)
            report['checks']['assignment_preservation'] = 'pass'
            progress.assignment = None; progress.switch('orchestration')
            prior_outputs = {r['id']: r for r in prior.get('outputs', []) if r.get('status') == 'pass'} if prior else {}
            common_preview = {k: v for k, v in manifest['preview'].items() if k != 'views'}
            for view in manifest['preview']['views']:
                progress.view = view['id']; progress.switch('preview_build_preflight')
                publish('preview:' + view['id'])
                dependencies = [a['id'] for a in manifest['assignments'] if a['target']['object'] in view['objects']]
                key = view_fingerprint(view, manifest, report['implementation'], params['expected_sha256'], task_keys)
                row = {'id': view['id'], 'lighting': view['lighting'], 'objects': view['objects'], 'fingerprint': key,
                       'status': 'not_run', 'reused': False}
                blocked = [d for d in dependencies if rows[d]['status'] != 'pass']
                if blocked:
                    row.update(code='DEPENDENCY_FAILED', error='Unsuccessful assignments: ' + ', '.join(blocked))
                else:
                    output = job / 'views' / view['id']; output.mkdir(parents=True)
                    cached = prior_outputs.get(view['id'])
                    if cached and cached.get('fingerprint') == key:
                        try:
                            doc = _artifact({'file': cached['file'], 'expected_sha256': cached['sha256']}, prior['_root'])
                            _guard(doc, guards)
                            dest = output / ('render-%06d.png' % manifest['context']['frame'])
                            shutil.copyfile(doc['file'], dest)
                            progress.switch('image_sheet')
                            decoded = rendering.image_report(dest)
                            if decoded['bytes'] > 2*1024*1024 or any(i['width'] != common_preview['width'] or i['height'] != common_preview['height'] for i in decoded['images']):
                                raise Failure('VALIDATION_FAILED', 'Cached view dimensions/size differ')
                            row.update(decoded, status='pass', reused=True, framing=cached.get('framing'), device=cached.get('device'), engine=manifest['preview']['engine'], frame=manifest['context']['frame'])
                            report['resume']['view_reuse'] += 1
                        except Exception as error:
                            row['cache_rejection'] = str(error)
                            # Never overwrite a partial cache copy; fresh rendering uses another directory.
                            output = output / 'fresh'; output.mkdir()
                    if row['status'] != 'pass':
                        progress.switch('preview_build_preflight')
                        rig = None
                        try:
                            single.activate(manifest['context'])
                            rig = batch_preview.build_preview(manifest, [scenes.find(bpy.data.objects, name) for name in view['objects']])
                            spec = single.render_spec(manifest, rig['scene'].name, rig['view_layer'].name, rig['camera'].name)
                            hashes = rendering.preflight({'file': report['candidate'], 'resources': resources}, job, spec, guards)
                            framing = batch_preview.configure_view(rig, view)
                            progress.switch('render_entry')
                            publish('rendering:' + view['id'])
                            rendered = rendering.run({'file': report['candidate'], 'manifest': spec}, output, prepared_hashes=hashes)
                            progress.switch('image_sheet')
                            image = rendered['outputs'][0]
                            if image['bytes'] > 2*1024*1024: raise Failure('RESOURCE_LIMIT', 'Preview exceeds 2 MiB')
                            row.update(image, status='pass', framing=framing, device=rendered['device'], engine=spec['engine'])
                        except Exception as error:
                            progress.switch('orchestration')
                            err = _err(error); row.update(status='failed', code=err['code'], error=err['message'])
                            if err['code'] in ('CONFLICT', 'CANCELLED', 'TIMEOUT'): raise
                        finally:
                            progress.switch('preview_build_preflight')
                            single.activate(manifest['context'])
                            if rig: batch_preview.cleanup(rig)
                report['outputs'].append(row); publish('view_complete:' + view['id'])
            progress.view = None; progress.switch('image_sheet')
            publish('contact_sheet')
            report['sheet'] = preview.compose_sheet(report['outputs'], job / 'contact-sheet.png', max_edge=min(2048, max(common_preview['width'], common_preview['height'])*3+64))
            progress.switch('final_guards')
            for path, (guard, sha) in resource_guards.items():
                if guard.sha256() != sha: raise Failure('CONFLICT', 'Input resource changed: ' + path)
            if digest(params['file']) != params['expected_sha256']: raise Failure('CONFLICT', 'Source changed during batch')
            report['checks'].update(source_sha_unchanged='pass', relative_resources='pass', image_decode=report['sheet']['status'])
            tasks_ok = all(r['status'] == 'pass' for r in report['assignments'])
            views_ok = all(r['status'] == 'pass' for r in report['outputs'])
            any_success = any(r['status'] == 'pass' for r in report['assignments'])
            report['status'] = 'pass' if tasks_ok and views_ok and report['sheet']['status'] == 'pass' else 'partial' if any_success else 'failed'
            report['limitations'] = ['Explicit local static mesh objects and per-assignment material instances; no implicit shared material fan-out',
                'Existing UV required; explicit face assignment rejects shared mesh edits',
                'Original source dependencies are shared preflight; new texture failures isolate their consumers',
                'Views render exactly their declared object subset; failed consumers block dependent views',
                'Scope/source/implementation/engine changes rebuild; compatible recipe changes update only affected assignments/views',
                'Validated checkpoints survive cancellation; unfinished views are rerendered',
                'Cycles and Eevee have distinct lighting semantics; GRAPHICS uses the host context without an exact GPU selector',
                'Only the bounded static shader node compatibility matrix is accepted; no instances or object-context effects']
            if not any_success: raise Failure('VALIDATION_FAILED', 'No material assignments succeeded; inspect batch-report.json')
            publish(report['status'])
    except Exception as error:
        progress.switch('orchestration')
        report.update(status='failed', error=_err(error))
        publish('failed')
        raise single.failure(error) from error
    finally:
        publish(report['status'])
    return report
