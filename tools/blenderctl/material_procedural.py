"""Protected procedural authoring/export in one owned Blender worker.

Author and reconstructed files are distinct new job artifacts. Existing files
and non-target datablocks are never used as output destinations.
"""
from contextlib import ExitStack
import copy
import os
from pathlib import Path
import time

import bpy

from protocol import Failure, atomic_json, digest
from material_procedural_contract import normalize_params, normalize_export_params, verify_input_hashes
from material_workflow import (activate, failure, localize_existing_resources, relative_resources,
                               render_spec, snapshot, stage_resources)
from material_workflow_addon import procedural_core, preview
from filesystem import FileGuard
from inspection import all_ids, identity
import rendering
import scenes


def _key(row):
    return row['type'], row['name'], row.get('library')


def _verify_preservation(before, after, target, mesh_name, additions, replacements=()):
    """Only explicitly owned new blocks and the declared object slot may differ.

Existing blocks are never filtered by name/managed tag. Thus an existing
shared group/image/material cannot be hidden by adding it to an allowlist.
"""
    left, right = copy.deepcopy(before), copy.deepcopy(after)
    original = {_key(row) for row in before['scene']['datablocks']}
    permitted = set(additions)
    if original & permitted:
        raise Failure('VALIDATION_FAILED', 'Preservation exemptions must contain new datablocks only')
    # Replacements are pre-authorized *before* construction from the verified,
    # exclusively bound target material/root group, never inferred afterwards.
    permitted.update(replacements)
    for state in (left, right):
        filtered = []
        for row in state['scene']['datablocks']:
            if _key(row) in permitted:
                continue
            if row['type'] == 'Mesh' and row['name'] == mesh_name:
                slots = row['mesh']['materials']
                while slots and slots[-1] is None:
                    slots.pop()
            filtered.append(row)
        state['scene']['datablocks'] = filtered
        for binding in state['bindings']:
            if binding['object'] == target['object']:
                slot = target['material_slot']
                binding['slots'] = [entry for i, entry in enumerate(binding['slots']) if i != slot]
    mismatch = scenes.compare(left, right)
    if mismatch:
        raise Failure('VALIDATION_FAILED', 'Non-target content changed: ' + mismatch)


def _target(manifest):
    target = manifest['target']
    obj = scenes.find(bpy.data.objects, target['object'])
    if obj.type != 'MESH' or obj.library or obj.data.library:
        raise Failure('UNSUPPORTED', 'Procedural material target must be a local mesh object')
    if obj.name not in bpy.context.view_layer.objects:
        raise Failure('INVALID_REQUEST', 'Target is outside the selected view layer')
    if target['uv_layer'] not in obj.data.uv_layers:
        raise Failure('NOT_FOUND', 'Target UV layer missing: ' + target['uv_layer'])
    if target['material_slot'] > len(obj.material_slots):
        raise Failure('INVALID_REQUEST', 'Target material slot cannot leave empty gaps')
    return obj


def _graph_blocks(material):
    groups, images, visited = set(), set(), set()

    def visit(tree):
        pointer = tree.as_pointer()
        if pointer in visited:
            return
        visited.add(pointer)
        for node in tree.nodes:
            image = getattr(node, 'image', None)
            if image:
                images.add(('Image', image.name, None))
            child = getattr(node, 'node_tree', None)
            if child:
                if child.library:
                    raise Failure('UNSUPPORTED', 'Linked procedural node groups are not supported')
                groups.add((child.bl_rna.identifier, child.name, None))
                visit(child)

    visit(material.node_tree)
    return groups, images


def _require_managed(material):
    result = procedural_core.verify_managed_state(material)
    if not isinstance(result, dict) or not result.get('ok'):
        raise Failure('CONFLICT', 'Procedural material state was modified: ' + str(result))
    return result


def _target_replacements(obj, manifest):
    slot = manifest['target']['material_slot']
    if slot >= len(obj.material_slots):
        return set()
    material = obj.material_slots[slot].material
    if not material or material.get('mwp_id') != manifest['material']['id']:
        return set()
    _require_managed(material)
    if material.library or material.override_library or obj.material_slots[slot].link != 'OBJECT':
        raise Failure('CONFLICT', 'Existing procedural target is not independently editable')
    if any(s.material == material and (other != obj or i != slot)
           for other in bpy.data.objects for i, s in enumerate(other.material_slots)):
        raise Failure('CONFLICT', 'Shared procedural material must be isolated before updating')
    allowed = {('Material', material.name, None)}
    group = bpy.data.node_groups.get(material.get('mwp_group_name', ''))
    if group and not group.library and group.users == 1:
        allowed.add((group.bl_rna.identifier, group.name, None))
    return allowed


def _owned_author_blocks(before, material, applied, job):
    original = {_key(row) for row in before['scene']['datablocks']}
    material_key = ('Material', material.name, None)
    if material_key in original:
        raise Failure('VALIDATION_FAILED', 'Authoring must create an independent material')
    groups, images = _graph_blocks(material)
    declared_group = material.get('mwp_group_name')
    if not declared_group or not any(key[1] == declared_group for key in groups):
        raise Failure('VALIDATION_FAILED', 'Procedural root group ownership is missing')
    allowed = {material_key} | (groups - original) | (images - original)
    for document in applied.get('generated_images', []):
        name = document if isinstance(document, str) else document.get('name')
        image = bpy.data.images.get(name) if name else None
        if image is None:
            raise Failure('VALIDATION_FAILED', 'Declared generated image was not created')
        key = ('Image', image.name, None)
        if key in original:
            raise Failure('VALIDATION_FAILED', 'Generated image reuses an existing datablock')
        if image.source == 'FILE':
            path = Path(bpy.path.abspath(image.filepath)).resolve()
            if not path.is_relative_to(job) or not path.is_file():
                raise Failure('VALIDATION_FAILED', 'Generated helper image must belong to this job')
        allowed.add(key)
    return allowed


def _resources(job, inherited=()):
    """Enumerate the actual saved static closure, including new helper maps."""
    documents = {str(Path(row['file']).resolve()): row['expected_sha256'] for row in inherited}
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT', 'COMPOSITING') or image.packed_file or image.source == 'GENERATED':
            continue
        if image.source != 'FILE':
            raise Failure('UNSUPPORTED', 'Only static file images or packed resources can be delivered')
        path = Path(bpy.path.abspath(image.filepath)).resolve()
        if not path.is_relative_to(job) or not path.is_file():
            raise Failure('VALIDATION_FAILED', 'Candidate image is outside its owned dependency directory: ' + str(path))
        sha = digest(path)
        if str(path) in documents and documents[str(path)] != sha:
            raise Failure('CONFLICT', 'Staged material resource changed')
        documents[str(path)] = sha
    return [{'file': path, 'expected_sha256': sha} for path, sha in sorted(documents.items())]


def _save_reopen(path, context, job, report, managed_material=None):
    if path.exists():
        raise Failure('CONFLICT', 'Material output already exists: ' + str(path))
    retained = []
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
            item.use_fake_user = True
            retained.append(identity(item))
    report.setdefault('retained_orphans', []).extend(retained)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=False, relative_remap=True, check_existing=False)
    relative_resources(job)
    expected = snapshot()
    atomic_json(job / (path.stem + '-expected.json'), expected)
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=False, relative_remap=True, check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    activate(context)
    mismatch = scenes.compare(expected, snapshot())
    if mismatch:
        raise Failure('VALIDATION_FAILED', 'Saved material candidate differs after reopening: ' + mismatch)
    if managed_material is not None:
        material = bpy.data.materials.get(managed_material)
        if material is None:
            raise Failure('VALIDATION_FAILED', 'Authored material disappeared after reopening')
        _require_managed(material)
    return {'file': str(path), 'expected_sha256': digest(path)}


def _progress(job, stage, report):
    atomic_json(job / 'procedural-progress.json', {'stage': stage,
                'completed_views': len(report['outputs']),
                'successful_views': sum(row.get('status') == 'pass' for row in report['outputs']),
                'latest_successful_image': next((row for row in reversed(report['outputs'])
                                                 if row.get('status') == 'pass'), None)})


def _render_views(candidate, manifest, resources, job, label, report):
    """Use the existing bounded studio only after a persistent candidate exists."""
    bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False, use_scripts=False)
    activate(manifest['context'])
    obj = _target(manifest)
    rig = preview.build_preview(manifest, obj)
    if manifest['preview']['engine'] == 'BLENDER_EEVEE':
        # This owned studio needs screen-space ray tracing to exercise the
        # authored transmission/refraction setting. It remains an Eevee
        # approximation, not parity with Cycles or an external engine.
        rig['scene'].eevee.use_raytracing = True
        report['eevee_preview'] = {'ray_tracing': True, 'method': rig['scene'].eevee.ray_tracing_method,
                                  'scope': 'screen-space reflection/refraction approximation'}
    spec = render_spec(manifest, rig['scene'].name, rig['view_layer'].name, rig['camera'].name)
    root = job / (label + '-views')
    root.mkdir()
    rows = []
    from material_outputs import output_check
    with ExitStack() as guards:
        prepared = {'file': candidate['file'], 'resources': resources}
        hashes = rendering.preflight(prepared, job, spec, guards)
        for view in manifest['preview']['views']:
            row = {'id': label + '_' + view['id'], 'view': view['id'], 'variant': label,
                   'lighting': view['lighting'], 'status': 'not_run'}
            _progress(job, 'rendering:' + row['id'], report)
            try:
                framing = preview.configure_view(rig, view)
                folder = root / view['id']
                folder.mkdir()
                rendered = rendering.run({'file': candidate['file'], 'manifest': spec}, folder,
                                         prepared_hashes=hashes)
                image = rendered['outputs'][0]
                if image['bytes'] > 2 * 1024 * 1024:
                    raise Failure('RESOURCE_LIMIT', 'Preview exceeds 2 MiB; choose a smaller preview')
                decoded = output_check(Path(image['file']))
                row.update(image, status='pass', framing=framing, decoded=decoded,
                           device=rendered['device'], engine=spec['engine'])
            except Exception as error:
                err = failure(error)
                row.update(status='failed', code=err.code, error=str(err))
                if err.code in ('CONFLICT', 'CANCELLED', 'TIMEOUT'):
                    report['outputs'].append(row)
                    raise err from error
            rows.append(row)
            report['outputs'].append(row)
            atomic_json(job / 'procedural-report.json', report)
    edge = min(2048, max(manifest['preview']['width'], manifest['preview']['height']) * 2 + 64)
    sheet = preview.compose_sheet(rows, job / (label + '-contact-sheet.png'), max_edge=edge)
    report.setdefault('sheets', {})[label] = sheet
    if not any(row['status'] == 'pass' for row in rows):
        raise Failure('VALIDATION_FAILED', 'All ' + label + ' diagnostic views failed')
    return 'pass' if all(row['status'] == 'pass' for row in rows) and sheet['status'] == 'pass' else 'partial'


def _export_files(value):
    """Read only actual output descriptors, not arbitrary strings in metadata."""
    files = set()
    if isinstance(value, dict):
        if isinstance(value.get('file'), str) and ('sha256' in value or 'expected_sha256' in value):
            path = Path(value['file']).resolve()
            sha = value.get('expected_sha256', value.get('sha256'))
            if not path.is_file() or digest(path) != sha:
                raise Failure('VALIDATION_FAILED', 'Exported file is missing or differs from its report')
            files.add(path)
        for child in value.values():
            files.update(_export_files(child))
    elif isinstance(value, list):
        for child in value:
            files.update(_export_files(child))
    return files


def _baked_image_blocks(before, exported, output_dir):
    originals = {_key(row) for row in before['scene']['datablocks']}
    allowed = set()
    files = _export_files(exported)
    if not files or any(not path.is_relative_to(output_dir) for path in files):
        raise Failure('VALIDATION_FAILED', 'Export report must describe files in the owned bake directory')
    for image in bpy.data.images:
        key = ('Image', image.name, None)
        if key in originals:
            continue
        if image.type == 'RENDER_RESULT':
            # The runtime-only Render Result does not persist in saved datablock snapshots.
            continue
        path = Path(bpy.path.abspath(image.filepath)).resolve()
        if path not in files:
            raise Failure('VALIDATION_FAILED', 'Undeclared image left by procedural bake/rebuild')
        allowed.add(key)
    return allowed, files


def _verify_rebuilt(material, files):
    groups, images = _graph_blocks(material)
    if groups:
        raise Failure('VALIDATION_FAILED', 'Reconstruction must not depend on authoring node groups')
    for node in material.node_tree.nodes:
        if node.bl_idname in ('ShaderNodeTexNoise', 'ShaderNodeTexVoronoi', 'ShaderNodeBevel',
                             'ShaderNodeAmbientOcclusion', 'ShaderNodeTexWave', 'ShaderNodeTexMusgrave'):
            raise Failure('VALIDATION_FAILED', 'Reconstruction retains a procedural source')
        image = getattr(node, 'image', None)
        if image:
            path = Path(bpy.path.abspath(image.filepath)).resolve()
            if image.source != 'FILE' or path not in files or not path.is_file():
                raise Failure('VALIDATION_FAILED', 'Reconstruction image is not a final disk export')
    if not images:
        raise Failure('VALIDATION_FAILED', 'Reconstruction contains no exported images')


def _export_current(params, manifest, job, report):
    from material_workflow_addon import procedural_bake
    activate(manifest['context'])
    obj = _target(manifest)
    slot = manifest['target']['material_slot']
    if slot >= len(obj.material_slots) or not obj.material_slots[slot].material:
        raise Failure('NOT_FOUND', 'Export target has no material')
    material = obj.material_slots[slot].material
    _require_managed(material)
    before = snapshot()
    _progress(job, 'baking', report)
    output_dir = job / 'baked'
    exported = procedural_bake.bake_channels(obj, material, output_dir, params['export'],
                                              uv_layer=manifest['target']['uv_layer'])
    report['export'] = exported
    if exported.get('status') != 'complete' or not exported.get('delivery', {}).get('complete'):
        raise Failure('VALIDATION_FAILED', 'Procedural channel bake did not pass')
    additions, files = _baked_image_blocks(before, exported, output_dir)
    _verify_preservation(before, snapshot(), manifest['target'], obj.data.name, additions)
    report['checks']['bake_non_target_preservation'] = 'pass'
    _progress(job, 'rebuilding', report)
    rebuilt = procedural_bake.rebuild_material(exported, name=material.name + '_Reconstructed')
    if ('Material', rebuilt.name, None) in {_key(row) for row in before['scene']['datablocks']}:
        raise Failure('VALIDATION_FAILED', 'Reconstruction must create a new independent material')
    _verify_rebuilt(rebuilt, files)
    obj.material_slots[slot].link = 'OBJECT'
    obj.material_slots[slot].material = rebuilt
    additions, files = _baked_image_blocks(before, exported, output_dir)
    additions.add(('Material', rebuilt.name, None))
    _verify_preservation(before, snapshot(), manifest['target'], obj.data.name, additions)
    report['checks']['rebuild_non_target_preservation'] = 'pass'
    resources = _resources(job, report.get('resources', []))
    report['reconstructed'] = _save_reopen(job / 'reconstructed.blend', manifest['context'], job, report)
    obj = _target(manifest)
    _verify_rebuilt(obj.material_slots[slot].material, files)
    report['checks']['reconstructed_saved_reopen'] = 'pass'
    report['checks']['disk_only_reconstruction'] = 'pass'
    report['reconstructed_material'] = obj.material_slots[slot].material.name
    report['resources'] = resources
    status = _render_views(report['reconstructed'], manifest, resources, job, 'reconstructed', report)
    report['checks']['reconstructed_render_decode'] = status
    report['comparison'] = {'status': 'requires_visual_review',
                            'scope': 'Matched saved author/reconstructed preview settings; no automatic aesthetic equivalence claim'}
    report['reconstruction_scope'] = 'Target material uses only exported maps/parameters; original author and non-target data are retained'
    return status


def _execute(params, job, export_only):
    job = Path(job).resolve()
    operation = 'material.procedural-export' if export_only else 'material.procedural-run'
    report = {'report_version': '1.0', 'operation': operation, 'status': 'running',
              'source_saved': False, 'checks': {}, 'outputs': [], 'limitations': [
                  'Static single-object, single-tile PBR; no external engine validation',
                  'Preview/reconstruction images require human aesthetic review'], 'resources': []}
    started = time.monotonic()
    try:
        params = normalize_export_params(params) if export_only else normalize_params(params)
        report['source'] = {'file': params['file'], 'expected_sha256': params['expected_sha256']}
        _progress(job, 'preflight', report)
        with ExitStack() as guards:
            for document in [report['source'], *params['resources']]:
                guard = guards.enter_context(FileGuard(document['file']))
                if guard.sha256() != document['expected_sha256']:
                    raise Failure('CONFLICT', 'Material input changed before execution')
            verify_input_hashes(params)
            bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
                raise Failure('UNSUPPORTED', 'Procedural material requires matching Blender major/minor')
            if bpy.data.sounds or bpy.data.movieclips or bpy.data.cache_files or bpy.data.volumes or bpy.data.libraries:
                raise Failure('UNSUPPORTED', 'Procedural material requires a local static dependency closure')
            if export_only:
                activate(params['context'])
                obj = _target(params)
                slot = params['target']['material_slot']
                if slot >= len(obj.material_slots) or not obj.material_slots[slot].material:
                    raise Failure('NOT_FOUND', 'Export target has no managed material')
                material = obj.material_slots[slot].material
                _require_managed(material)
                manifest = copy.deepcopy(procedural_core.get_stored_manifest(material))
                manifest['context'] = copy.deepcopy(params['context'])
                manifest['target'] = copy.deepcopy(params['target'])
            else:
                manifest = params['manifest']
                activate(manifest['context'])
                obj = _target(manifest)
            check_spec = render_spec(manifest, manifest['context']['scene'],
                                     manifest['context']['view_layer'], 'preflight')
            rendering.preflight(params, job, check_spec, guards)
            before = snapshot()
            atomic_json(job / 'procedural-source-snapshot.json', before)
            mapping, resources = stage_resources(params, job)
            if not export_only:
                _progress(job, 'authoring', report)
                replacements = _target_replacements(obj, manifest)
                paths = {row['file']: mapping[os.path.normcase(row['file'])] for row in params['resources']}
                applied = procedural_core.apply_material(manifest, resource_paths=paths, work_dir=job / 'helpers')
                material = bpy.data.materials[applied['material_name']]
                _require_managed(material)
                allowed = _owned_author_blocks(before, material, applied, job)
                _verify_preservation(before, snapshot(), manifest['target'], obj.data.name, allowed, replacements)
                report['material'] = applied
                report['checks']['author_non_target_preservation'] = 'pass'
            localize_existing_resources(mapping)
            report['resources'] = _resources(job, resources)
            report['author'] = _save_reopen(job / 'author.blend', manifest['context'], job, report,
                                           managed_material=material.name)
            report['checks']['author_saved_reopen'] = 'pass'
            atomic_json(job / 'procedural-manifest.json', manifest)
            author_status = _render_views(report['author'], manifest, report['resources'], job, 'author', report)
            report['checks']['author_render_decode'] = author_status
            export_status = 'pass'
            if export_only or params.get('export') is not None:
                bpy.ops.wm.open_mainfile(filepath=report['author']['file'], load_ui=False, use_scripts=False)
                export_status = _export_current(params, manifest, job, report)
            verify_input_hashes(params)
            report['checks']['source_and_resource_sha_unchanged'] = 'pass'
            report['status'] = 'pass' if author_status == export_status == 'pass' else 'partial'
            report['environment'] = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode()}
            atomic_json(job / 'procedural-resources.json', report['resources'])
            _progress(job, report['status'], report)
    except Exception as error:
        err = failure(error)
        report['status'] = 'failed'
        report['error'] = {'code': err.code, 'message': str(err)}
        _progress(job, 'failed', report)
        raise err from error
    finally:
        report['seconds'] = time.monotonic() - started
        atomic_json(job / 'procedural-report.json', report)
    return report


def run(params, job):
    return _execute(params, job, False)


def export(params, job):
    return _execute(params, job, True)
