# SPDX-License-Identifier: GPL-3.0-or-later
"""One protected material candidate and diagnostic views in one Blender worker."""
import copy
import json
import os
from pathlib import Path
import shutil
import time

import bpy
from contextlib import ExitStack
from protocol import Failure, CODES, atomic_json, digest
from material_workflow_contract import normalize_params
from material_workflow_addon import core, preview
from material_workflow_addon.contract import WorkflowError
import nodes
import rendering
import scenes
from inspection import all_ids, identity


def activate(context):
    scene = scenes.find(bpy.data.scenes, context['scene'])
    layer = scenes.find(scene.view_layers, context['view_layer'])
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    scene.frame_set(context['frame'])
    layer.update()
    if bpy.context.mode != 'OBJECT':
        raise Failure('UNSUPPORTED', 'Material workflow requires Object Mode')
    return scene, layer


def render_spec(manifest, scene, layer, camera):
    p = manifest['preview']
    return {'scene': scene, 'view_layer': layer, 'camera': camera,
            **{k: p[k] for k in ('engine', 'device', 'width', 'height', 'samples', 'denoise', 'color')},
            'frames': [manifest['context']['frame']], 'format': 'PNG', 'depth': '8',
            'transparent': False, 'compositor': False, 'passes': [], 'motion_blur': 0}


def snapshot():
    return {'scene': scenes.state(), 'bindings': [
        {'object': o.name, 'slots': [{'material': s.material.name if s.material else None,
                                     'link': s.link} for s in o.material_slots]}
        for o in sorted(bpy.data.objects, key=lambda o: o.name) if o.type == 'MESH']}


def verify_preservation(before, after, manifest, mesh_name, created_images=()):
    """Compare covered source content, allowing only the declared slot/material."""
    mid = manifest['material']['id']
    a, b = copy.deepcopy(before), copy.deepcopy(after)
    def filter_blocks(state):
        result = {}
        for row in state['scene'].pop('datablocks'):
            managed = row['custom_properties'].get(core.KEY, '')
            if row['type'] == 'Material' and managed == mid: continue
            if row['type'] == 'Image' and managed.startswith(mid + '/'): continue
            if row['type'] == 'Image' and row['name'] in created_images: continue
            if row['type'] == 'Mesh' and row['name'] == mesh_name:
                # Core only appends empty data slots; OBJECT overrides are checked below.
                slots = row['mesh'].pop('materials')
                while slots and slots[-1] is None: slots.pop()
                row['mesh']['materials'] = slots
            result[(row['type'], row['name'])] = row
        return result
    old, new = filter_blocks(a), filter_blocks(b)
    if set(old) != set(new):
        raise Failure('VALIDATION_FAILED', 'Unexpected non-target datablock additions/removals')
    for key in old:
        mismatch = scenes.compare(old[key], new[key])
        if mismatch:
            raise Failure('VALIDATION_FAILED', 'Non-target content changed: ' + str(key) + ' ' + mismatch)
    for state in (a, b):
        for row in state['bindings']:
            if row['object'] == manifest['target']['object']:
                slot = manifest['target']['material_slot']
                row['slots'] = [s for i, s in enumerate(row['slots']) if i != slot]
    mismatch = scenes.compare(a, b)
    if mismatch: raise Failure('VALIDATION_FAILED', 'Non-target scene/binding changed: ' + mismatch)


def new_target_images(material, before_images, manifest):
    """Allow only new images actually used by the declared v2 material."""
    from material_workflow_addon.contract import image_documents
    expected = {(r['expected_sha256'], r['color_space']) for r in image_documents(manifest)}
    names = set()
    for node in material.node_tree.nodes:
        image = getattr(node, 'image', None)
        if image is None or image in before_images: continue
        color = image.colorspace_settings.name
        sha = digest(bpy.path.abspath(image.filepath))
        if (sha, color) not in expected or image.get(core.KEY) != 'mw_image_v2/' + sha + '/' + color:
            raise Failure('VALIDATION_FAILED', 'New image is not a declared v2 material resource')
        names.add(image.name)
    return names


def stage_resources(params, job):
    folder = job / 'textures'
    folder.mkdir()
    mapping, staged = {}, {}
    for resource in params['resources']:
        source = Path(resource['file'])
        sha = resource['expected_sha256']
        if digest(source) != sha: raise Failure('CONFLICT', 'Resource changed before staging: ' + str(source))
        # Content-addressed names avoid unsafe/colliding basenames and very long paths.
        dest = folder / (sha + source.suffix.lower())
        if not dest.exists(): shutil.copyfile(source, dest)
        if digest(dest) != sha: raise Failure('VALIDATION_FAILED', 'Staged resource copy differs')
        mapping[os.path.normcase(str(source.resolve()))] = str(dest)
        staged[str(dest)] = sha
    return mapping, [{'file': p, 'expected_sha256': h} for p, h in staged.items()]


def localize_existing_resources(mapping):
    """Rebase static external image/font bytes without changing their contents."""
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT', 'COMPOSITING') or image.packed_file: continue
        if image.source == 'GENERATED': continue
        if image.source != 'FILE': raise Failure('UNSUPPORTED', 'M1 packaging supports static FILE images only')
        source = os.path.normcase(str(Path(bpy.path.abspath(image.filepath)).resolve()))
        if source not in mapping: raise Failure('INVALID_REQUEST', 'Undeclared image during packaging: ' + source)
        image.filepath = mapping[source]
        if nodes.RESOURCE_KEY in image:
            image[nodes.RESOURCE_KEY] = json.dumps([{'file': mapping[source], 'expected_sha256': digest(mapping[source]), 'number': 0}])
    for font in bpy.data.fonts:
        if font.filepath == '<builtin>' or font.packed_file: continue
        source = os.path.normcase(str(Path(bpy.path.abspath(font.filepath)).resolve()))
        if source not in mapping: raise Failure('INVALID_REQUEST', 'Undeclared font during packaging')
        font.filepath = mapping[source]


def relative_resources(job):
    for item in [*bpy.data.images, *bpy.data.fonts]:
        if getattr(item, 'packed_file', None): continue
        path = getattr(item, 'filepath', '')
        if not path or path == '<builtin>': continue
        absolute = Path(bpy.path.abspath(path)).resolve()
        if absolute.is_relative_to(job): item.filepath = bpy.path.relpath(str(absolute))


def failure(error):
    if isinstance(error, Failure): return error
    if isinstance(error, WorkflowError):
        code = error.code
        if code not in CODES:
            code = ('CONFLICT' if code in ('managed_conflict', 'shared_slot', 'shared_material', 'resource_sha_mismatch', 'group_sha_mismatch')
                    else 'NOT_FOUND' if code in ('missing_resource', 'missing_group')
                    else 'UNSUPPORTED' if code in ('structure_change_unsupported', 'unsupported_image', 'protected_target', 'unsupported_group', 'unsupported_engine', 'unsupported_node')
                    else 'INVALID_REQUEST')
        return Failure(code, str(error))
    return Failure('VALIDATION_FAILED', str(error))


def save_template(params, job):
    """Export a checked shader recipe and explicit local instance bindings."""
    from material_workflow_contract import normalize_template_save_params
    from material_workflow_addon import templates
    from material_workflow_addon.contract import image_documents, normalize_manifest
    from filesystem import FileGuard
    job = Path(job).resolve()
    started = time.monotonic()
    report = {'report_version': '1.0', 'operation': 'template-save', 'status': 'failed', 'source_saved': False,
              'workflow_report': str(job / 'workflow-report.json'), 'checks': {}}
    try:
        params = normalize_template_save_params(params)
        with ExitStack() as guards:
            source_guard = guards.enter_context(FileGuard(params['file']))
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Source SHA differs')
            bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
                raise Failure('UNSUPPORTED', 'Template export requires matching Blender major/minor')
            declared = {}
            for resource in params['resources']:
                guard = guards.enter_context(FileGuard(resource['file']))
                if guard.sha256() != resource['expected_sha256']:
                    raise Failure('CONFLICT', 'Resource SHA differs: ' + resource['file'])
                declared[os.path.normcase(resource['file'])] = resource['expected_sha256']
            nodes.resource_guards(params, guards)
            matches = [m for m in bpy.data.materials if m.get(core.KEY) == params['material_id']]
            if len(matches) != 1:
                raise Failure('CONFLICT' if matches else 'NOT_FOUND', 'Managed material ID must resolve uniquely')
            before = snapshot()
            manifest = core.export_current_manifest(matches[0])
            for image in image_documents(manifest):
                if declared.get(os.path.normcase(image['file'])) != image['expected_sha256']:
                    raise Failure('INVALID_REQUEST', 'Exported texture is not explicitly declared: ' + image['file'])
            template = templates.template_from_manifest(manifest, params['template_id'], params['name'], params['version'])
            bindings = {key: copy.deepcopy(manifest[key]) for key in ('context', 'target', 'preview')}
            bindings.update(material={key: manifest['material'][key] for key in ('id', 'name')}, resources={}, groups={})
            for layer in manifest['layers']:
                for channel, resource in layer['channels'].items():
                    bindings['resources'][layer['id'] + '_' + channel] = copy.deepcopy(resource)
                if layer['mask']:
                    bindings['resources'][layer['id'] + '_mask'] = copy.deepcopy(layer['mask'])
                if layer.get('effect'):
                    bindings['groups'][layer['id'] + '_effect'] = {k: layer['effect'][k] for k in ('group', 'expected_sha256')}
            # Round-trip the export through the same host-safe instantiation contract.
            resolved = templates.instantiate_template(template, bindings)
            expected = copy.deepcopy(manifest)
            expected['schema_version'] = '1.1'; expected['material']['template'] = 'pbr_layers_v2'
            if resolved != normalize_manifest(expected):
                raise Failure('VALIDATION_FAILED', 'Template round-trip differs')
            mismatch = scenes.compare(before, snapshot())
            if mismatch: raise Failure('VALIDATION_FAILED', 'Export changed source content: ' + mismatch)
            templates.write_template(job / 'template.json', template)
            atomic_json(job / 'bindings.json', bindings)
            atomic_json(job / 'resources.json', params['resources'])
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Source changed during template export')
            report.update(status='pass', template=str(job / 'template.json'), bindings=str(job / 'bindings.json'),
                          resources=str(job / 'resources.json'), source={'file': params['file'], 'expected_sha256': params['expected_sha256']},
                          template_sha256=digest(job / 'template.json'), bindings_sha256=digest(job / 'bindings.json'),
                          limitations=['Template stores logical slots; bindings reference existing local texture files',
                                       'Custom groups must already exist with matching content in the target project',
                                       'Independent manual nodes and layout are not exported as template layers'])
            report['checks'] = {'source_content_preservation': 'pass', 'source_sha_unchanged': 'pass', 'template_instantiation': 'pass'}
    except Exception as error:
        err = failure(error)
        report['error'] = {'code': err.code, 'message': str(err)}
        raise err from error
    finally:
        report['seconds'] = time.monotonic() - started
        atomic_json(job / 'workflow-report.json', report)
    return report


def run(params, job):
    started = time.monotonic()
    job = Path(job).resolve()
    report = {'report_version': '1.0', 'status': 'failed', 'candidate': None,
              'report': str(job / 'workflow-report.json'), 'outputs': [], 'sheet': None,
              'checks': {}, 'source_saved': False}
    def progress(stage):
        atomic_json(job / 'workflow-progress.json', {'stage': stage, 'completed_views': len(report['outputs'])})
    try:
        params = normalize_params(params)
        manifest = params['manifest']
        report['source'] = {'file': params['file'], 'expected_sha256': params['expected_sha256']}
        if digest(params['file']) != params['expected_sha256']: raise Failure('CONFLICT', 'Source SHA differs')
        progress('preflight')
        bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
        if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
            raise Failure('UNSUPPORTED', 'Material workflow requires matching Blender major/minor')
        activate(manifest['context'])
        # These media need package adapters beyond the static material M1 contract.
        if bpy.data.sounds or bpy.data.movieclips or bpy.data.cache_files or bpy.data.volumes:
            raise Failure('UNSUPPORTED', 'M1 source contains media/cache/volume dependencies requiring a package adapter')
        check_spec = render_spec(manifest, manifest['context']['scene'], manifest['context']['view_layer'], 'preflight')
        with ExitStack() as guards:
            rendering.preflight(params, job, check_spec, guards)
            obj = scenes.find(bpy.data.objects, manifest['target']['object'])
            if obj.type != 'MESH': raise Failure('INVALID_REQUEST', 'M1 target must be a mesh')
            mesh_name = obj.data.name
            before = snapshot()
            before_images = set(bpy.data.images)
            atomic_json(job / 'source-snapshot.json', before)
            mapping, resources = stage_resources(params, job)
            # Pass the original spellings too: normalize_params normalizes case-preserving paths.
            transport = {r['file']: mapping[os.path.normcase(r['file'])] for r in params['resources']}
            progress('material')
            applied = core.apply_material(manifest, transport)
            created_images = (new_target_images(bpy.data.materials[applied['material_name']], before_images, manifest)
                              if manifest['schema_version'] == '1.1' else ())
            verify_preservation(before, snapshot(), manifest, mesh_name, created_images)
            report['checks']['source_content_preservation'] = 'pass'
            # Managed images already point into the staging directory.
            for r in resources: mapping[os.path.normcase(r['file'])] = r['file']
            localize_existing_resources(mapping)
            retained = []
            for item in all_ids():
                if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
                    item.use_fake_user = True; retained.append(identity(item))
            candidate = job / 'candidate.blend'
            if candidate.exists(): raise Failure('CONFLICT', 'Candidate already exists')
            bpy.context.preferences.filepaths.save_version = 0
            # Establish Main's new base before storing relative resource paths.
            bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=False, relative_remap=True, check_existing=False)
            relative_resources(job)
            material = bpy.data.materials[applied['material_name']]
            expected_material = core.capture_managed_state(material)
            expected = snapshot()
            atomic_json(job / 'candidate-expected.json', expected)
            bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=False, relative_remap=True, check_existing=False)
            progress('reopen')
            bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
            activate(manifest['context'])
            observed = snapshot()
            mismatch = scenes.compare(expected, observed)
            if mismatch: raise Failure('VALIDATION_FAILED', 'Saved candidate differs: ' + mismatch)
            material = bpy.data.materials[applied['material_name']]
            if core.capture_managed_state(material) != expected_material:
                raise Failure('VALIDATION_FAILED', 'Managed shader differs after reopening')
            for image in bpy.data.images:
                if image.source == 'FILE' and not image.packed_file:
                    if not image.filepath.startswith('//') or not Path(bpy.path.abspath(image.filepath)).resolve().is_relative_to(job):
                        raise Failure('VALIDATION_FAILED', 'Candidate texture is not a relative packaged dependency')
            report.update(candidate=str(candidate), candidate_sha256=digest(candidate), material=applied,
                          resources=resources, resources_file=str(job / 'resources.json'), retained_orphans=retained)
            atomic_json(job / 'resources.json', resources)
            atomic_json(job / 'manifest.json', manifest)
            report['checks']['saved_reopen'] = 'pass'
            report['checks']['relative_texture_paths'] = 'pass'
            report['checks']['source_sha_unchanged'] = 'pass' if digest(params['file']) == params['expected_sha256'] else 'fail'
            if report['checks']['source_sha_unchanged'] != 'pass': raise Failure('CONFLICT', 'Source changed during workflow')
            atomic_json(job / 'workflow-report.json', report)
            obj = bpy.data.objects[manifest['target']['object']]
            rig = preview.build_preview(manifest, obj)
            spec = render_spec(manifest, rig['scene'].name, rig['view_layer'].name, rig['camera'].name)
            prepared = {'file': str(candidate), 'resources': resources}
            # Full candidate and generated-scene preflight before the internal prepared path.
            hashes = rendering.preflight(prepared, job, spec, guards)
            output_root = job / 'views'; output_root.mkdir()
            dependency_error = None
            for view in manifest['preview']['views']:
                row = {'id': view['id'], 'lighting': view['lighting'], 'status': 'not_run'}
                if dependency_error:
                    row['error'] = dependency_error
                else:
                    try:
                        progress('rendering:' + view['id'])
                        framing = preview.configure_view(rig, view)
                        folder = output_root / view['id']; folder.mkdir()
                        rendered = rendering.run({'file': str(candidate), 'manifest': spec}, folder, prepared_hashes=hashes)
                        image = rendered['outputs'][0]
                        if image['bytes'] > 2 * 1024 * 1024:
                            raise Failure('RESOURCE_LIMIT', 'Preview exceeds 2 MiB; use a smaller preview size')
                        row.update(image, status='pass', framing=framing, device=rendered['device'], engine=spec['engine'])
                    except Exception as error:
                        err = failure(error)
                        row.update(status='failed', code=err.code, error=str(err))
                        if err.code in ('CONFLICT', 'CANCELLED', 'TIMEOUT'):
                            dependency_error = 'Shared dependency failure: ' + str(err)
                report['outputs'].append(row)
                atomic_json(job / 'workflow-report.json', report)
            progress('contact_sheet')
            edge = min(2048, max(manifest['preview']['width'], manifest['preview']['height']) * 2 + 64)
            sheet = preview.compose_sheet(report['outputs'], job / 'contact-sheet.png', max_edge=edge)
            report['sheet'] = sheet
            passed = sum(o['status'] == 'pass' for o in report['outputs'])
            report['status'] = 'pass' if passed == len(report['outputs']) and sheet['status'] == 'pass' else 'partial' if passed else 'failed'
            report['checks']['render_decode'] = report['status']
            report['limitations'] = [*applied['limitations'], *rig['limitations'],
                'Existing verified UV required; generic automatic UV repair is not provided',
                'Cycles/Eevee use a bounded static surface-node subset; GRAPHICS uses the host context without exact GPU selection',
                'Candidate contains authored source scene; temporary preview studio is not saved',
                'Technical rendering checks do not certify material aesthetics']
            if manifest['schema_version'] == '1.0':
                report['limitations'].append('V1 baseline uses absolute image paths; upgrade to v2 before relocating for managed updates')
            report['environment'] = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode()}
            if not passed: raise Failure('VALIDATION_FAILED', 'All material diagnostic views failed; inspect workflow-report.json')
            progress(report['status'])
    except Exception as error:
        err = failure(error)
        report['error'] = {'code': err.code, 'message': str(err)}
        report['status'] = 'failed'
        progress('failed')
        raise err from error
    finally:
        report['seconds'] = time.monotonic() - started
        atomic_json(job / 'workflow-report.json', report)
    return report
