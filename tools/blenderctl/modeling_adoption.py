"""Review a Modeling Workflow report and append its verified scene to a saved copy."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))
sys.path.insert(0, str(ENTRY.parent.parent))

from filesystem import FileGuard
from modeling_bridge_review import (bridge_datablocks, bridge_inputs, bridge_provenance, is_bridge_report,
                                   review_bridge_candidate, verify_bridge_append)
from processes import ProcessTree
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure,
                      atomic_json, digest, envelope, read_json, timeout_value)


STUDIO_FBX_ADAPTER = 'STUDIO_FBX_STATIC_V1'
STUDIO_GLB_PROVENANCE = 'mw_studio_glb_export'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def parser():
    result = Parser(description=__doc__)
    result.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    result.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    result.add_argument('--transactions-dir', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--timeout', type=float, default=120)
    output = result.add_mutually_exclusive_group()
    output.add_argument('--compact', action='store_true', help='JSON envelope (already compact)')
    output.add_argument('--full', action='store_true', help='JSON envelope including artifact paths')
    output.add_argument('--human', action='store_true', help='One-line result')
    actions = result.add_subparsers(dest='action', required=True, parser_class=Parser)
    review = actions.add_parser('review-candidate', help='Review a native import or Studio disk or Bridge handoff')
    review.add_argument('report', type=Path)
    review.add_argument('--expected-report-sha256', help='Expected report SHA-256; required for a Studio GLB or Bridge handoff')
    adopt = actions.add_parser('adopt-candidate', help='Append a verified Modeling or Studio scene to a new work copy',
                               description='Append one verified candidate as a local scene in a new copy of the saved target.',
                               epilog='For a Studio Bridge handoff, retain the original receiver and the sibling candidate.blend. '
                                      'Continue with data.output, data.scene and the original-to-local names in data.bridge_id_map.')
    adopt.add_argument('source', type=Path, help='A saved .blend snapshot including the edits to carry forward')
    adopt.add_argument('--expected-sha256', required=True, help='SHA-256 of the saved source')
    adopt.add_argument('--report', type=Path, required=True, help='Reviewed native import or Studio disk or Bridge handoff report')
    adopt.add_argument('--expected-report-sha256', required=True, help='SHA-256 from the candidate review')
    adopt.add_argument('--scene', help='Use this source scene as the active context before appending')
    for command in (review, adopt):
        command.add_argument('--studio-export-manifest', type=Path,
                             help='Bind a released static FBX Studio handoff report to its saved export manifest')
        command.add_argument('--expected-studio-export-sha256', help='SHA-256 of the saved Studio export manifest')
    return result


def check_sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise Failure('INVALID_REQUEST', f'{label} must be a 64-character lowercase SHA-256')
    return value


def worker_check(bpy):
    flags = {'--factory-startup', '--disable-autoexec', '--offline-mode'}
    if (not bpy.app.background or not flags.issubset(sys.argv) or bpy.app.online_access
            or bpy.context.preferences.filepaths.use_scripts_auto_execute):
        raise Failure('UNSUPPORTED', 'Modeling workers require offline factory startup with auto-execution disabled')
    return {'version': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}


def unpack_report(report):
    if not isinstance(report, dict) or report.get('producer') != 'modeling_workflow_v1' or report.get('operation') != 'import_local':
        raise Failure('INVALID_REQUEST', 'Expected an import_local Modeling Workflow report')
    if report.get('fresh_process_reopen') != 'pass' or report.get('save_reopen') != 'pass':
        raise Failure('VALIDATION_FAILED', 'Candidate requires successful saved and fresh-process reopen checks')
    candidate = report.get('candidate')
    if not isinstance(candidate, str) or not Path(candidate).is_absolute():
        raise Failure('INVALID_REQUEST', 'Report candidate must be an absolute .blend path')
    if not isinstance(report.get('scene_name'), str) or not report['scene_name']:
        raise Failure('INVALID_REQUEST', 'Report candidate scene is missing')
    content = report.get('content')
    if not isinstance(content, dict) or any(not isinstance(content.get(key), list) for key in ('objects', 'meshes', 'materials', 'images')):
        raise Failure('INVALID_REQUEST', 'Report requires complete candidate content from native import')
    previews = report.get('previews', [])
    if not isinstance(previews, list) or len(previews) > 4:
        raise Failure('INVALID_REQUEST', 'Report previews must be a list of up to four records')
    for item in previews:
        if (not isinstance(item, dict) or not isinstance(item.get('file'), str) or not Path(item['file']).is_absolute()
                or not isinstance(item.get('view'), str)
                or any(type(item.get(key)) is not int or item[key] <= 0 for key in ('width', 'height'))):
            raise Failure('INVALID_REQUEST', 'Invalid preview record')
    return previews


def studio_request(report, export, protect):
    if not isinstance(report, dict) or report.get('ok') is not True or report.get('mode') != 'fbx':
        raise Failure('INVALID_REQUEST', 'Expected a successful released Studio FBX handoff')
    fbx = report.get('fbx_import_report')
    inspection = report.get('inspect')
    if (not isinstance(fbx, dict) or not isinstance(inspection, dict) or inspection.get('ok') is not True
            or not isinstance(inspection.get('data'), dict) or fbx.get('ok') is not True
            or report.get('inspect_exit') != 0 or report.get('blender_exit') != 0):
        raise Failure('INVALID_REQUEST', 'Expected a successful released Studio FBX handoff and separate inspection')
    if fbx.get('armature_count') != 0 or fbx.get('actions') != []:
        raise Failure('UNSUPPORTED', 'Use the Studio FBX handoff and rigging or animation tools for non-static candidates')
    meshes = fbx.get('meshes')
    if (not isinstance(meshes, list) or not meshes or len(meshes) != fbx.get('mesh_count')
            or any(not isinstance(row, dict) or not isinstance(row.get('name'), str)
                   or not {'vertices', 'faces', 'uv_layers', 'material_slots'} <= row.keys() for row in meshes)
            or len({row['name'] for row in meshes}) != len(meshes)):
        raise Failure('INVALID_REQUEST', 'Studio handoff requires unique native mesh observations')
    if (report.get('orientation_transform_applied') is not False or report.get('front_axis') not in ('+X', '-X', '+Y', '-Y')
            or fbx.get('front_axis') != report['front_axis']):
        raise Failure('INVALID_REQUEST', 'Expected a declared front axis with no transform applied to the Studio output')
    doc = read_json(export['file'])
    artifact = doc.get('artifact') if isinstance(doc, dict) else None
    if (not isinstance(artifact, dict) or artifact.get('ok') is not True
            or artifact.get('format') != 'fbx' or artifact.get('kind') != 'FBX_BINARY'
            or not isinstance(artifact.get('path'), str) or not Path(artifact['path']).is_absolute()
            or not all(isinstance(doc.get(key), str) and doc[key] for key in ('project_id', 'operator_id', 'download_job'))):
        raise Failure('INVALID_REQUEST', 'Expected a completed FBX export with distinct Studio project and operator identities')
    matching = (doc['project_id'] == report.get('project_id') == fbx.get('project_id')
                and artifact.get('path') == report.get('input_file') == fbx.get('input_fbx')
                and artifact.get('sha256') == report.get('input_sha256') == fbx.get('source_sha256'))
    if not matching:
        raise Failure('CONFLICT', 'Studio export and Blender handoff refer to different projects or exported bytes')
    candidate_path = report.get('candidate_blend')
    if (not isinstance(candidate_path, str) or candidate_path != fbx.get('output_blend')
            or candidate_path != inspection['data'].get('file')):
        raise Failure('CONFLICT', 'Studio Blender handoff and inspection refer to different work files')
    original, _ = protect(artifact['path'], '.fbx', check_sha(artifact['sha256'], 'Studio FBX SHA'), from_report=True)
    if Path(original['file']).stat().st_size != artifact.get('bytes'):
        raise Failure('CONFLICT', 'Studio FBX size differs from the export manifest')
    return {'export': export, 'input': original, 'project_id': doc['project_id'], 'operator_id': doc['operator_id'],
            'download_job': doc['download_job'], 'packaging': artifact.get('packaging'), 'format': 'fbx',
            'front_axis': report['front_axis']}


def native_studio_fbx_inputs(report, protect):
    source = report.get('input')
    source_file = source.get('file') if isinstance(source, dict) else None
    is_fbx = isinstance(source_file, str) and Path(source_file).suffix.lower() == '.fbx'
    if not (is_fbx or {'studio_fbx', 'native_normals'}.intersection(report)
            or report.get('input_adapter') == STUDIO_FBX_ADAPTER):
        return None
    studio = report.get('studio_fbx')
    normals = report.get('native_normals')
    if (not is_fbx or report.get('input_adapter') != STUDIO_FBX_ADAPTER or not isinstance(studio, dict)
            or not isinstance(studio.get('export'), dict) or not isinstance(normals, list) or not normals):
        raise Failure('INVALID_REQUEST', 'Expected a native Studio FBX report with its source, export and normal records')
    export = studio['export']
    identifiers = {key: studio.get(key) for key in ('project_id', 'operator_id', 'download_job')}
    if not isinstance(export.get('file'), str) or not all(isinstance(value, str) and value for value in identifiers.values()):
        raise Failure('INVALID_REQUEST', 'Expected the Studio export manifest and its project, operator and download identity')
    locked_source, _ = protect(source_file, '.fbx', check_sha(source.get('expected_sha256'), 'FBX input SHA'),
                               from_report=True)
    locked_export, _ = protect(export['file'], '.json', check_sha(export.get('expected_sha256'), 'Studio export SHA'),
                               from_report=True)

    from modeling_workflow_contract import read_studio_export
    binding = read_studio_export({**identifiers, 'file': locked_export['file'], 'expected_sha256': locked_export['sha256']},
                                 {'file': locked_source['file'], 'expected_sha256': locked_source['sha256']})
    if studio != binding:
        raise Failure('CONFLICT', 'Native Studio report differs from the bound export manifest')
    return {'source': locked_source, 'binding': binding, 'normals': normals}


def studio_glb_inputs(report, handoff, protect):
    """Resolve the Studio disk receipt and the exact Modeling report it embeds."""
    from modeling_workflow_contract import read_studio_export

    studio = report['studio_export']
    job = report.get('modeling_job')
    inspected = report.get('inspect')
    if (report.get('ok') is not True or report.get('blenderctl_exit') != 0 or report.get('inspect_exit') != 0
            or report.get('orientation_transform_applied') is not False
            or not isinstance(studio, dict) or not isinstance(job, dict) or not isinstance(inspected, dict)
            or job.get('ok') is not True or job.get('command') != 'modeling.run' or inspected.get('ok') is not True):
        raise Failure('INVALID_REQUEST', 'Expected a successful Studio GLB disk handoff, import and inspection')
    identifiers = {key: studio.get(key) for key in ('project_id', 'operator_id', 'download_job')}
    artifacts = job.get('artifacts')
    native_file = artifacts.get('modeling-report.json') if isinstance(artifacts, dict) else None
    if not all(isinstance(value, str) and value for value in
               (*identifiers.values(), studio.get('manifest_file'), report.get('input_file'), native_file)):
        raise Failure('INVALID_REQUEST', 'Studio GLB handoff requires the original files and export identities')
    if (report.get('project_id') != identifiers['project_id'] or studio.get('local_sha256') != report.get('input_sha256')
            or studio.get('format') != 'gltf' or studio.get('packaging') != 'embedded'):
        raise Failure('CONFLICT', 'Studio GLB handoff disagrees with its recorded source or project')
    source, _ = protect(report['input_file'], '.glb', check_sha(report.get('input_sha256'), 'Studio GLB SHA'), from_report=True)
    export, _ = protect(studio['manifest_file'], '.json', check_sha(studio.get('manifest_sha256'), 'Studio export SHA'),
                        from_report=True)
    binding = read_studio_export({**identifiers, 'file': export['file'], 'expected_sha256': export['sha256']},
                                 {'file': source['file'], 'expected_sha256': source['sha256']})
    native_ref, _ = protect(native_file, '.json', from_report=True)
    native = read_json(native_ref['file'])
    previews = unpack_report(native)
    mirrored = job.get('data')
    if (native.get('version') != '0.1.14' or native.get('status') != 'passed' or not isinstance(mirrored, dict)
            or {key: value for key, value in mirrored.items() if key != 'input_hashes'} != native):
        raise Failure('CONFLICT', 'Native Modeling report differs from the Studio GLB handoff')
    candidate_sha = check_sha(native.get('candidate_sha256'), 'Studio GLB candidate SHA')
    inspect_data = inspected.get('data')
    if (native.get('input') != {'file': source['file'], 'expected_sha256': source['sha256']}
            or native.get('candidate') != report.get('candidate_blend')
            or candidate_sha != report.get('candidate_blend_sha256')
            or not isinstance(inspect_data, dict) or inspect_data.get('file') != native['candidate']
            or inspect_data.get('source_sha256') != candidate_sha):
        raise Failure('CONFLICT', 'Studio GLB import and inspection refer to different input or candidate files')
    return native, previews, {'handoff': handoff, 'import_report': native_ref, 'input': source, 'export': binding}


def inputs(args, stack, checkpoint):
    directories = set()

    def protect(path, suffix, expected=None, from_report=False):
        requested = Path(os.path.abspath(path))
        resolved = requested.resolve(strict=True)
        if resolved.suffix.lower() != suffix or requested != resolved:
            raise Failure('INVALID_REQUEST', f'Expected a real {suffix} file without redirected path components: {requested}')
        if from_report and not Path(path).is_absolute():
            raise Failure('INVALID_REQUEST', 'Report artifact paths must be absolute')
        for parent in reversed(resolved.parents):
            if parent == Path(resolved.anchor) or parent in directories:
                continue
            stack.enter_context(FileGuard(parent, directory=True))
            directories.add(parent)
        guard = stack.enter_context(FileGuard(resolved))
        if suffix == '.json' and resolved.stat().st_size > 16 * 1024 * 1024:
            raise Failure('INVALID_REQUEST', 'Candidate report exceeds 16 MiB')
        actual = guard.sha256(checkpoint)
        if expected is not None and actual != check_sha(expected, str(resolved)):
            raise Failure('CONFLICT', f'Input changed since review or snapshot: {resolved}')
        return {'file': str(resolved), 'sha256': actual}, guard.identity()

    def with_target(request, protected_inputs):
        if args.action == 'adopt-candidate':
            source, identity = protect(args.source, '.blend', args.expected_sha256)
            if identity in protected_inputs:
                raise Failure('CONFLICT', 'Use a saved work project distinct from the candidate and its original receiver')
            request.update(source=source, scene=args.scene, state_source='SAVED_DISK_V1')
        return request

    report_ref, _ = protect(args.report, '.json', args.expected_report_sha256)
    report = read_json(report_ref['file'])
    if is_bridge_report(report):
        if args.studio_export_manifest is not None or args.expected_studio_export_sha256 is not None:
            raise Failure('INVALID_REQUEST', 'Use the Studio Bridge handoff and its SHA, without the disk FBX export arguments')
        if args.expected_report_sha256 is None:
            raise Failure('INVALID_REQUEST', 'Use the original Studio Bridge handoff SHA from its publication receipt')
        candidate, bridge, identities = bridge_inputs(report, report_ref, protect, check_sha)
        return with_target({'action': args.action, 'report': report_ref, 'candidate': candidate, 'bridge': bridge}, identities)
    studio = None
    studio_glb = None
    if bool(args.studio_export_manifest) != bool(args.expected_studio_export_sha256):
        raise Failure('INVALID_REQUEST', 'Studio handoff needs both the export manifest and its SHA-256')
    if isinstance(report, dict) and str(report.get('mode')).casefold() == 'glb' and 'studio_export' in report:
        if args.expected_report_sha256 is None or args.studio_export_manifest is not None:
            raise Failure('INVALID_REQUEST', 'Use the Studio GLB handoff report and its SHA without separate export options')
        native, previews, studio_glb = studio_glb_inputs(report, report_ref, protect)
        candidate_path, candidate_sha = native['candidate'], native['candidate_sha256']
    elif args.studio_export_manifest is not None:
        export, _ = protect(args.studio_export_manifest, '.json', args.expected_studio_export_sha256)
        studio = studio_request(report, export, protect)
        candidate_path, candidate_sha, previews = report['candidate_blend'], report.get('candidate_blend_sha256'), []
    else:
        previews = unpack_report(report)
        candidate_path, candidate_sha = report['candidate'], report.get('candidate_sha256')
    candidate, candidate_identity = protect(candidate_path, '.blend', check_sha(candidate_sha, 'candidate_sha256'), from_report=True)
    for row in previews:
        protect(row['file'], '.png', check_sha(row.get('sha256'), 'preview sha256'), from_report=True)
    request = {'action': args.action, 'report': report_ref, 'candidate': candidate}
    if studio is not None:
        request['studio'] = studio
    elif studio_glb is not None:
        request['studio_glb'] = studio_glb
    else:
        native_fbx = native_studio_fbx_inputs(report, protect)
        if native_fbx is not None:
            request['native_studio_fbx'] = native_fbx
    return with_target(request, [candidate_identity])


def scene_state(names):
    """Scoped preservation witness for original scenes and their authored membership."""
    import bpy
    from inspection import identity, key, record

    def collection_membership(collection):
        return {'name': collection.name, 'objects': sorted((identity(obj) for obj in collection.objects), key=key),
                'children': sorted((identity(child) for child in collection.children), key=key)}

    result = {}
    for name in names:
        scene = bpy.data.scenes.get(name)
        if scene is None:
            raise Failure('VALIDATION_FAILED', f'Original scene missing: {name}')
        objects = sorted(scene.objects, key=lambda obj: key(identity(obj)))
        dependencies = {obj.data for obj in objects if obj.data is not None}
        dependencies.update(slot.material for obj in objects for slot in obj.material_slots if slot.material is not None)
        result[name] = {'scene': record(scene), 'frame_current': scene.frame_current,
                        'view_layers': [layer.name for layer in scene.view_layers],
                        'objects': [record(obj) for obj in objects],
                        'data': sorted((record(block) for block in dependencies), key=key),
                        'collections': [collection_membership(scene.collection), *sorted(
                            (collection_membership(c) for c in scene.collection.children_recursive), key=key)]}
    state = {'scenes': result, 'workspaces': sorted(item.name for item in bpy.data.workspaces)}
    return hashlib.sha256(key(state).encode('utf-8')).hexdigest()


def external_references():
    import bpy

    return sorted({os.path.normcase(os.path.normpath(path))
                   for path in bpy.utils.blend_paths(absolute=True, packed=False, local=False) if path})


def verify_original_references(expected, candidate):
    baseline = set(expected)
    observed = set(external_references())
    candidate = os.path.normcase(os.path.normpath(candidate))
    # Blender can record the source file as weak provenance for a local append.
    if not baseline <= observed or observed - baseline - {candidate}:
        raise Failure('VALIDATION_FAILED', 'Append changed the original external path set')


def require_supported_file_version(bpy):
    if bpy.data.version[:2] > bpy.app.version[:2]:
        version = '.'.join(str(field) for field in bpy.data.version[:2])
        raise Failure('UNSUPPORTED', f'Select Blender {version} or a compatible newer build for this file')


def candidate_content_sha(content):
    """Compare reviewed content across a local append, which can rename Blender IDs."""
    from inspection import key

    def without_names(row, names):
        return {field: value for field, value in row.items() if field not in names}

    meshes = [{**without_names(row, {'object', 'mesh', 'parent', 'materials'}),
               'parented': row['parent'] is not None,
               'occupied_slots': [name is not None for name in row['materials']]}
              for row in content['meshes']]
    materials = [{**without_names(row, {'name', 'nodes'}),
                  'nodes': [{**without_names(node, {'image'}), 'has_image': node.get('image') is not None}
                            for node in row['nodes']]} for row in content['materials']]
    objects = [{**without_names(row, {'name', 'parent'}), 'parented': row['parent'] is not None}
               for row in content['objects']]
    images = [without_names(row, {'name'}) for row in content['images']]
    payload = [sorted(group, key=key) for group in (meshes, materials, objects, images)]
    return hashlib.sha256(key(payload).encode('utf-8')).hexdigest()


def studio_fbx_evidence(metadata, scene, core, normals):
    provenance = json.loads(scene.get(core.OWNER, '{}'))
    if provenance.get('source_sha256') != metadata['source']['sha256']:
        raise Failure('CONFLICT', 'Studio FBX source SHA differs from the native candidate provenance')
    return {'input_adapter': STUDIO_FBX_ADAPTER, 'fbx_source': metadata['source'],
            'studio_fbx': metadata['binding'], 'native_normals': normals}


def verify_appended_studio_fbx(metadata, scene, names, core):
    source_names = {row['object'] for row in metadata['normals']}
    scene_names = {obj.name for obj in scene.objects if obj.type == 'MESH'}
    if set(names) != source_names or len(names) != len(scene_names) or set(names.values()) != scene_names:
        raise Failure('VALIDATION_FAILED', 'Adopted Studio FBX does not match the reviewed mesh identity map')
    normals = sorted(({**row, 'object': names[row['object']]} for row in metadata['normals']),
                     key=lambda row: row['object'])
    core.verify_studio_fbx(metadata['binding'], normals, scene)
    return studio_fbx_evidence(metadata, scene, core, normals)


def glb_source_identity(scene, owner):
    try:
        properties = json.loads(scene.get(owner, 'null'))
    except (TypeError, ValueError):
        properties = None
    if not isinstance(properties, dict) or not isinstance(properties.get('provenance'), dict):
        raise Failure('CONFLICT', 'Native GLB candidate is missing its saved source identity')
    if 'studio_fbx' in properties:
        raise Failure('CONFLICT', 'Native Studio FBX candidate requires its bound FBX report')
    return {'sha256': properties.get('source_sha256'), 'provenance': properties['provenance']}


def review_candidate(request, bpy, core):
    if 'bridge' in request:
        return review_bridge_candidate(request, bpy, require_supported_file_version)
    if 'studio' in request:
        return review_studio_candidate(request, bpy, core)
    studio_glb = request.get('studio_glb')
    reference = studio_glb['import_report'] if studio_glb else request['report']
    report, _ = core.load_report(reference['file'], reference['sha256'])
    native_fbx = request.get('native_studio_fbx')
    options = {'studio_fbx': native_fbx['binding'], 'native_normals': native_fbx['normals']} if native_fbx else {}
    try:
        content = core.verify_candidate(request['candidate']['file'], report['content'], **options)
    except ValueError as exc:
        origin = report.get('blender')
        if origin and origin != bpy.app.version_string:
            raise Failure('UNSUPPORTED', f'{exc}. Review with the report Blender ({origin}) or regenerate the candidate for this Blender.') from exc
        raise
    require_supported_file_version(bpy)
    if bpy.context.scene.name != report['scene_name']:
        raise Failure('VALIDATION_FAILED', 'Candidate report scene differs from the active scene observed on reopen')
    if bpy.data.libraries or external_references():
        raise Failure('VALIDATION_FAILED', 'Imported Modeling candidate must be self-contained')
    evidence = {}
    if native_fbx:
        evidence = studio_fbx_evidence(native_fbx, bpy.context.scene, core, native_fbx['normals'])
    else:
        source = report.get('input')
        source_sha = check_sha(source.get('expected_sha256') if isinstance(source, dict) else None,
                               'GLB input SHA')
        identity = glb_source_identity(bpy.context.scene, core.OWNER)
        if identity != {'sha256': source_sha, 'provenance': report.get('provenance')}:
            raise Failure('CONFLICT', 'Native GLB candidate source SHA or provenance differs from the import report')
        evidence['glb_source'] = identity
        if studio_glb:
            if source_sha != studio_glb['input']['sha256']:
                raise Failure('CONFLICT', 'Native Studio GLB candidate provenance differs from the bound export')
            evidence['studio_glb'] = studio_glb
    preview_results = []
    for row in report.get('previews', []):
        preview = bpy.data.images.load(row['file'], check_existing=False)
        size = list(preview.size)
        bpy.data.images.remove(preview)
        if size != [row['width'], row['height']]:
            raise Failure('VALIDATION_FAILED', 'Decoded preview dimensions differ from report')
        preview_results.append({'view': row['view'], 'file': row['file'], 'sha256': row['sha256'], 'size': size})
    return {'report': request['report'], 'candidate': request['candidate'], 'scene': report['scene_name'],
            'summary': {'meshes': len(content['meshes']), 'triangles': sum(m['triangles'] for m in content['meshes']),
                        'images': len(content['images']), 'visual_review': report.get('visual_review', 'pending'),
                        'task_id': report.get('provenance', {}).get('task_id', '')},
            'previews': preview_results, 'native_candidate_reopen': 'pass',
            'content_sha256_ignoring_id_renames': candidate_content_sha(content), **evidence}


def review_studio_candidate(request, bpy, core):
    metadata = request['studio']
    report = read_json(request['report']['file'])['fbx_import_report']
    if report.get('blender_version') != '.'.join(map(str, bpy.app.version)):
        raise Failure('UNSUPPORTED', 'Review the Studio handoff in its Blender version or recapture the disk handoff')
    if bpy.ops.wm.open_mainfile(filepath=request['candidate']['file'], load_ui=False, use_scripts=False) != {'FINISHED'}:
        raise Failure('VALIDATION_FAILED', 'Independent reader could not reopen the Studio FBX work file')
    require_supported_file_version(bpy)
    scene = bpy.context.scene
    seen = [scene.get(key) for key in ('tripo_studio_source_file', 'tripo_studio_source_sha256',
                                      'tripo_studio_project_id', 'tripo_studio_front_axis')]
    if seen != [metadata['input']['file'], metadata['input']['sha256'], metadata['project_id'], metadata['front_axis']]:
        raise Failure('CONFLICT', 'Native Studio candidate provenance differs from the bound export')
    if scene.get('tripo_studio_orientation_transform_applied') != False or bpy.data.libraries or external_references():
        raise Failure('VALIDATION_FAILED', 'Studio candidate has external dependencies or unrecorded axis changes')
    if bpy.data.actions or any(o.type not in ('MESH', 'EMPTY') or o.animation_data or o.vertex_groups
                               for o in scene.objects):
        raise Failure('UNSUPPORTED', 'Static Studio adoption does not qualify armatures, animation or deformation data')
    expected = {m['name']: (m['vertices'], m['faces'], m['uv_layers'], m['material_slots']) for m in report['meshes']}
    content = core.snapshot(scene)
    actual = {m['object']: (m['vertices'], m['faces'], [uv['name'] for uv in m['uv']], m['materials'])
              for m in content['meshes']}
    if not actual or len(actual) != report['mesh_count'] or actual != expected:
        raise Failure('VALIDATION_FAILED', 'Independent reader found different mesh, UV or material-slot observations')
    return {'report': request['report'], 'candidate': request['candidate'], 'scene': scene.name, 'studio': metadata,
            'summary': {'meshes': len(actual), 'triangles': sum(m['triangles'] for m in content['meshes']),
                        'images': len(content['images']), 'visual_review': 'pending'},
            'previews': [], 'native_candidate_reopen': 'pass',
            'content_sha256_ignoring_id_renames': candidate_content_sha(content)}


def append_studio_scene(request, reviewed, bpy):
    previous = bpy.context.scene
    if bpy.context.mode != 'OBJECT':
        raise Failure('CONFLICT', 'Switch to Object Mode before adopting a candidate')
    inventory = {}
    if 'bridge' in request:
        witness = read_json(request['report']['file'])['verification']
        inventory = bridge_datablocks(witness)
    elif 'native_studio_fbx' in request:
        inventory = {'objects': [row['object'] for row in request['native_studio_fbx']['normals']]}
    prior = {group: set(getattr(bpy.data, group)) for group in inventory}
    with bpy.data.libraries.load(request['candidate']['file'], link=False) as (source, destination):
        if reviewed['scene'] not in source.scenes:
            raise Failure('NOT_FOUND', 'Reviewed Studio scene is missing from the candidate')
        destination.scenes = [reviewed['scene']]
        for group, names in inventory.items():
            if not set(names).issubset(getattr(source, group)):
                raise Failure('CONFLICT', f'Reviewed {group} are missing from the Studio Bridge candidate')
            # Blender replaces elements of the assigned list with loaded RNA IDs.
            setattr(destination, group, list(names))
    adopted = destination.scenes[0]
    if adopted is None:
        raise Failure('WORKER_FAILED', 'Failed to append the reviewed Studio scene')
    adopted['mw_adoption_report_sha256'] = request['report']['sha256']
    adopted['mw_previous_scene'] = previous.name
    result = {'scene': adopted.name, 'previous_scene': previous.name, 'source_saved': False, 'current_file_saved': False}
    if 'studio' in request:
        adopted['mw_studio_export_sha256'] = request['studio']['export']['sha256']
        adopted['mw_studio_operator_id'] = request['studio']['operator_id']
    else:
        names = {}
        for group, requested in inventory.items():
            loaded = getattr(destination, group)
            if len(loaded) != len(requested) or any(block is None or block.library or block in prior[group] for block in loaded):
                raise Failure('CONFLICT', f'Expected newly appended local {group} for the Studio scene')
            names[group] = {original: block.name for original, block in zip(requested, loaded, strict=True)}
        if 'bridge' in request:
            adopted['mw_bridge_provenance'] = json.dumps(bridge_provenance(request), ensure_ascii=False, sort_keys=True)
            verify_bridge_append(request, bpy, adopted, names)
            result['bridge_id_map'] = names
        else:
            result['studio_fbx_object_map'] = names['objects']
    bpy.context.window.scene = adopted
    return result


def adopt(request, job, bpy, core):
    reviewed = review_candidate(request, bpy, core)
    if 'bridge' in request:
        reviewed['content_sha256_ignoring_id_renames'] = candidate_content_sha(core.snapshot(bpy.context.scene))
    source = request['source']
    if bpy.ops.wm.open_mainfile(filepath=source['file'], load_ui=True, use_scripts=False) != {'FINISHED'}:
        raise Failure('VALIDATION_FAILED', 'Unable to reopen the saved source snapshot')
    require_supported_file_version(bpy)
    if request['scene'] is not None:
        selected = bpy.data.scenes.get(request['scene'])
        if selected is None:
            raise Failure('NOT_FOUND', f'Source scene is missing: {request["scene"]}')
        bpy.context.window.scene = selected
    original_scenes = sorted(scene.name for scene in bpy.data.scenes)
    original_hash = scene_state(original_scenes)
    reference_paths = external_references()
    studio_glb = request.get('studio_glb')
    if {'studio', 'bridge', 'native_studio_fbx'}.intersection(request):
        adopted = append_studio_scene(request, reviewed, bpy)
    else:
        reference = studio_glb['import_report'] if studio_glb else request['report']
        adopted = core.adopt_candidate(reference['file'], reference['sha256'])
    scene = bpy.data.scenes[adopted['scene']]
    if studio_glb:
        scene['mw_adoption_report_sha256'] = request['report']['sha256']
        scene[STUDIO_GLB_PROVENANCE] = json.dumps(studio_glb, ensure_ascii=False, sort_keys=True)
        adopted['studio_glb'] = studio_glb
    if scene.name in original_scenes or scene_state(original_scenes) != original_hash:
        raise Failure('VALIDATION_FAILED', 'Append altered recorded original scene members or state')
    verify_original_references(reference_paths, request['candidate']['file'])
    if scene.library or any(obj.library or obj.data and obj.data.library for obj in scene.objects):
        raise Failure('VALIDATION_FAILED', 'Appended model has linked scene, object or data-block references')
    content = core.snapshot(scene)
    if candidate_content_sha(content) != reviewed['content_sha256_ignoring_id_renames']:
        raise Failure('VALIDATION_FAILED', 'Adopted scene differs from the reviewed content')
    if 'native_studio_fbx' in request:
        adopted.update(verify_appended_studio_fbx(request['native_studio_fbx'], scene,
                                                  adopted['studio_fbx_object_map'], core))
    if bpy.ops.wm.save_as_mainfile(filepath=str(job / 'staged-adoption.blend'), check_existing=False,
                                   relative_remap=True) != {'FINISHED'}:
        raise Failure('WORKER_FAILED', 'Unable to save the new candidate work copy')
    atomic_json(job / 'adoption-content.json', content)
    return {**adopted, 'current_file_saved': True, 'source': source, 'state_source': 'SAVED_DISK_V1',
            'reviewed': reviewed, 'original_scenes': original_scenes, 'original_scenes_sha256': original_hash,
            'original_external_references': reference_paths}


def verify_adoption(request, job, bpy, core):
    authored = read_json(job / 'author-result.json')['data']
    staged = job / 'staged-adoption.blend'
    if bpy.ops.wm.open_mainfile(filepath=str(staged), load_ui=True, use_scripts=False) != {'FINISHED'}:
        raise Failure('VALIDATION_FAILED', 'Independent reader could not reopen the adopted work copy')
    expected_names = sorted([*authored['original_scenes'], authored['scene']])
    if sorted(scene.name for scene in bpy.data.scenes) != expected_names:
        raise Failure('VALIDATION_FAILED', 'Independent reader found a different scene set')
    adopted = bpy.data.scenes[authored['scene']]
    if (adopted.get('mw_adoption_report_sha256') != request['report']['sha256']
            or adopted.get('mw_previous_scene') != authored['previous_scene']
            or bpy.context.scene != adopted):
        raise Failure('VALIDATION_FAILED', 'Independent reader found a different adoption identity or active scene')
    studio = request.get('studio')
    if studio and (adopted.get('mw_studio_export_sha256') != studio['export']['sha256']
                   or adopted.get('mw_studio_operator_id') != studio['operator_id']
                   or adopted.get('tripo_studio_project_id') != studio['project_id']
                   or adopted.get('tripo_studio_source_sha256') != studio['input']['sha256']):
        raise Failure('VALIDATION_FAILED', 'Independent reader found different Studio export identity in the adopted work copy')
    studio_glb = request.get('studio_glb')
    if studio_glb and adopted.get(STUDIO_GLB_PROVENANCE) != json.dumps(studio_glb, ensure_ascii=False, sort_keys=True):
        raise Failure('VALIDATION_FAILED', 'Independent reader found different Studio GLB provenance in the adopted work copy')
    glb_source = authored['reviewed'].get('glb_source')
    if glb_source is not None and glb_source_identity(adopted, core.OWNER) != glb_source:
        raise Failure('VALIDATION_FAILED', 'Independent reader found a different native GLB source identity')
    if 'bridge' in request:
        verify_bridge_append(request, bpy, adopted, authored['bridge_id_map'])
    if scene_state(authored['original_scenes']) != authored['original_scenes_sha256']:
        raise Failure('VALIDATION_FAILED', 'Independent reader found changed original scene members or state')
    verify_original_references(authored['original_external_references'], request['candidate']['file'])
    if core.snapshot(adopted) != read_json(job / 'adoption-content.json'):
        raise Failure('VALIDATION_FAILED', 'Independent reader found different adopted scene contents')
    result = {'status': 'pass', 'process': 'fresh_process', 'staged_sha256': digest(staged),
              'adopted_scene': adopted.name, 'original_scenes': authored['original_scenes'], 'original_external_references': 'pass',
              'original_scenes_sha256': authored['original_scenes_sha256']}
    if studio:
        result['studio_export_identity'] = {'export_sha256': adopted['mw_studio_export_sha256'],
                                           'operator_id': adopted['mw_studio_operator_id'],
                                           'project_id': adopted['tripo_studio_project_id'],
                                           'input_sha256': adopted['tripo_studio_source_sha256']}
    if studio_glb:
        result['studio_glb'] = studio_glb
    if glb_source is not None:
        result['glb_source'] = glb_source
    if 'bridge' in request:
        result['bridge_provenance'] = bridge_provenance(request)
    if 'native_studio_fbx' in request:
        names = authored['studio_fbx_object_map']
        result.update(verify_appended_studio_fbx(request['native_studio_fbx'], adopted, names, core),
                      studio_fbx_object_map=names)
    return result


def worker(phase, request_file):
    import bpy
    from modeling_workflow_addon import core

    job = request_file.parent
    try:
        runtime = worker_check(bpy)
        request = read_json(request_file)['params']
        if phase == 'review':
            data = review_candidate(request, bpy, core)
        elif phase == 'author':
            data = adopt(request, job, bpy, core)
        else:
            data = verify_adoption(request, job, bpy, core)
        result = {'ok': True, 'data': {**data, 'blender': runtime}, 'error': None}
    except (Failure, ValueError, OSError, RuntimeError) as exc:
        result = {'ok': False, 'data': None,
                  'error': {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}}
    atomic_json(job / f'{phase}-result.json', result)


def run_worker(blender, job, phase, deadline, state, checkpoint):
    argv = [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
            '--threads', '2', '--python-exit-code', '23', '--python', str(ENTRY), '--', phase, str(job / 'request.json')]
    state.update(phase=phase, heartbeat_at=time.time())
    atomic_json(job / 'status.json', state)
    profile = job / f'{phase}-user'
    profile.mkdir()
    env = {**os.environ, 'BLENDER_USER_RESOURCES': str(profile),
           'BLENDER_USER_CONFIG': str(profile / 'config'), 'BLENDER_USER_SCRIPTS': str(profile / 'scripts'),
           'BLENDER_USER_DATAFILES': str(profile / 'datafiles'), 'BLENDER_USER_EXTENSIONS': str(profile / 'extensions')}
    with (job / f'{phase}.log').open('xb') as log:
        tree = ProcessTree(argv, cwd=str(job), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            while True:
                checkpoint()
                try:
                    exit_code = tree.process.wait(timeout=min(2, max(.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    state['heartbeat_at'] = time.time()
                    atomic_json(job / 'status.json', state)
        except BaseException:
            tree.terminate()
            raise
        finally:
            tree.close()
    checkpoint()
    result_path = job / f'{phase}-result.json'
    if exit_code or not result_path.is_file():
        raise Failure('WORKER_FAILED', f'{phase} exited {exit_code}. See {phase}.log')
    result = read_json(result_path)
    if not result['ok']:
        raise Failure(result['error']['code'], result['error']['message'])
    return result['data']


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    blender = args.blender.resolve(strict=True)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    command = 'modeling.' + args.action
    state = {'state': 'running', 'job_id': job.name, 'pid': os.getpid(), 'heartbeat_at': time.time(), 'phase': 'verify_inputs'}
    atomic_json(job / 'status.json', state)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    data = None
    error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Cancelled before publishing an adopted work copy')
        if time.monotonic() >= deadline:
            raise Failure('TIMEOUT', 'Modeling work unit exceeded its timeout')

    try:
        with ExitStack() as stack:
            request = inputs(args, stack, checkpoint)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': command, 'params': request})
            if args.action == 'review-candidate':
                data = run_worker(blender, job, 'review', deadline, state, checkpoint)
            else:
                authored = run_worker(blender, job, 'author', deadline, state, checkpoint)
                staged = job / 'staged-adoption.blend'
                with FileGuard(staged) as guard:
                    verified = run_worker(blender, job, 'verify', deadline, state, checkpoint)
                    if guard.sha256(checkpoint) != verified['staged_sha256']:
                        raise Failure('CONFLICT', 'Staged work copy changed during independent verification')
                output = job / 'output-adopted.blend'
                with FileGuard(staged, renameable=True) as guard:
                    if guard.sha256(checkpoint) != verified['staged_sha256']:
                        raise Failure('CONFLICT', 'Staged work copy changed before publishing the work copy')
                    guard.rename_new(output)
                artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
                data = {**authored, 'output': {'file': str(output), 'sha256': verified['staged_sha256']},
                        'independent_reopen': verified, 'source_unchanged': True}
    except (Failure, OSError, RuntimeError) as exc:
        code = getattr(exc, 'code', 'NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR')
        error = {'code': code, 'message': str(exc)}
    for log in sorted(job.glob('*.log')):
        artifacts[log.name] = str(log)
    result = envelope(command, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', result)
    terminal = {'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}
    state.update(state=terminal.get(error['code'], 'failed') if error else 'succeeded',
                 heartbeat_at=time.time(), phase='done')
    atomic_json(job / 'status.json', state)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        code = getattr(exc, 'code', 'NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR')
        result = envelope(error={'code': code, 'message': str(exc)})
    if args is not None and args.human:
        if result['ok']:
            print(f'{result["command"]} succeeded: {result["artifacts"]["result"]}')
        else:
            print(f'{result["error"]["code"]}: {result["error"]["message"]}')
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--' in sys.argv and '--background' in sys.argv:
        worker_phase, worker_request = sys.argv[sys.argv.index('--') + 1:]
        worker(worker_phase, Path(worker_request))
    else:
        sys.exit(main())
