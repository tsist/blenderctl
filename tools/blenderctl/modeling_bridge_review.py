"""Read a published Studio Bridge handoff using its original native witness."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import re
from uuid import UUID

from protocol import Failure, read_json


BRIDGE_WITNESS = (Path(__file__).resolve().parents[2] /
                  '.agents/skills/tripo-studio-opencli/scripts/tripo_bridge_candidate.py')
IDENTITY_NAMES = ('task_id', 'step_key')
IDENTITY_UUIDS = ('project_id', 'source_operator_id', 'export_operator_id')
WITNESS_FIELDS = ('objects', 'materials', 'base_color_maps')


def is_bridge_report(report):
    return isinstance(report, dict) and {'received_file', 'received_sha256', 'candidate', 'identity'} <= report.keys()


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def bridge_inputs(report, report_ref, protect, check_sha):
    if type(report.get('schema_version')) is not int or report['schema_version'] != 1:
        raise Failure('UNSUPPORTED', 'Unknown Studio Bridge handoff version')
    if report.get('source') not in ('archived_receiver', 'bound_receiver'):
        raise Failure('INVALID_REQUEST', 'Expected an archived_receiver or bound_receiver Studio Bridge handoff')
    identity = report['identity']
    if (not isinstance(identity, dict) or identity.keys() != set(IDENTITY_NAMES + IDENTITY_UUIDS)
            or any(not isinstance(identity[field], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', identity[field])
                   for field in IDENTITY_NAMES)
            or any(not canonical_uuid(identity[field]) for field in IDENTITY_UUIDS)
            or identity['source_operator_id'] == identity['export_operator_id']):
        raise Failure('INVALID_REQUEST', 'Bridge handoff requires one task, step, project, source and export identity')
    recipe = {'project_id': identity['project_id'], 'model_format': 'fbx', 'fbx_preset': 'blender',
              'texture_size': 8192, 'texture_packaging': 'embedded'}
    if recipe != report.get('recipe'):
        raise Failure('CONFLICT', 'Bridge handoff recipe differs from the transfer identity or the supported static FBX recipe')
    if (report['candidate'] != 'candidate.blend'
            or not isinstance(report['received_file'], str) or not Path(report['received_file']).is_absolute()):
        raise Failure('INVALID_REQUEST', 'Bridge handoff requires its sibling candidate.blend and an absolute saved receiver path')
    verified = report.get('verification')
    if (not isinstance(verified, dict)
            or any(verified.get(field) is not True for field in ('cold_reopened', 'source_unchanged', 'scene_and_objects_match'))):
        raise Failure('VALIDATION_FAILED', 'Studio Bridge candidate is missing its successful independent reopen')
    scene, objects = report.get('scene'), report.get('objects')
    if (not isinstance(scene, str) or not scene or not isinstance(objects, list) or not objects
            or any(not isinstance(obj, str) or not obj for obj in objects)
            or objects != sorted(set(objects)) or not isinstance(verified.get('objects'), dict)
            or sorted(verified['objects']) != objects or not isinstance(verified.get('materials'), dict)
            or not isinstance(verified.get('base_color_maps'), list) or not verified['base_color_maps']
            or not isinstance(verified.get('blender_build'), str) or not verified['blender_build']
            or not isinstance(report.get('original_qualifications'), dict)):
        raise Failure('INVALID_REQUEST', 'Bridge handoff is missing its exact scene, object or original verification records')
    received, receiver_identity = protect(report['received_file'], '.blend',
                                          check_sha(report['received_sha256'], 'Bridge receiver SHA'), from_report=True)
    candidate_path = Path(report_ref['file']).with_name('candidate.blend')
    candidate, candidate_identity = protect(candidate_path, '.blend',
                                            check_sha(report.get('sha256'), 'Bridge candidate SHA'), from_report=True)
    if receiver_identity == candidate_identity:
        raise Failure('CONFLICT', 'The isolated Bridge candidate and receiver must be distinct saved files')
    bridge = {'identity': identity, 'recipe': recipe, 'source': report['source'], 'received': received,
              'producer_build': verified['blender_build'], 'original_qualifications': report['original_qualifications']}
    return candidate, bridge, [candidate_identity, receiver_identity]


def native_reader():
    if not BRIDGE_WITNESS.is_file():
        raise Failure('UNSUPPORTED', 'Workspace Studio Bridge native witness reader is unavailable')
    spec = importlib.util.spec_from_file_location('studio_bridge_native_witness', BRIDGE_WITNESS)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def review_bridge_candidate(request, bpy, require_supported_file_version):
    from projects import file_paths

    verifier = native_reader()
    report = read_json(request['report']['file'])
    expected = {field: report['verification'][field] for field in WITNESS_FIELDS}
    names, scene_name = report['objects'], report['scene']

    for label, descriptor in [('receiver', request['bridge']['received']), ('candidate', request['candidate'])]:
        if bpy.ops.wm.open_mainfile(filepath=descriptor['file'], load_ui=False, use_scripts=False) != {'FINISHED'}:
            raise Failure('VALIDATION_FAILED', f'Independent reader could not reopen the Studio Bridge {label}')
        require_supported_file_version(bpy)
        scene = bpy.data.scenes.get(scene_name)
        if scene is None:
            raise Failure('CONFLICT', f'Studio Bridge {label} is missing the recorded scene: {scene_name}')
        if label == 'candidate' and (scene != bpy.context.scene or len(bpy.data.scenes) != 1
                                     or sorted(bpy.data.objects.keys()) != names
                                     or bpy.data.libraries or file_paths()):
            raise Failure('VALIDATION_FAILED', 'Studio Bridge candidate no longer reopens as the isolated local scene')
        try:
            observed = verifier._scene_state(bpy, scene, names)
        except ValueError as exc:
            raise Failure('CONFLICT', f'Studio Bridge {label} differs from the original native witness: {exc}') from exc
        for field in WITNESS_FIELDS:
            if observed[field] != expected[field]:
                raise Failure('CONFLICT', f'Studio Bridge {label} differs from the original native witness: {field}')

    meshes = [obj.data for obj in scene.objects if obj.type == 'MESH']
    for mesh in meshes:
        mesh.calc_loop_triangles()
    images = {row['name'] for material in observed['materials'].values() for row in material['images'].values()}
    content_sha = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {'report': request['report'], 'candidate': request['candidate'], 'scene': scene_name,
            'candidate_saved_version': list(bpy.data.version),
            'summary': {'meshes': len(meshes), 'triangles': sum(len(mesh.loop_triangles) for mesh in meshes),
                        'images': len(images), 'visual_review': 'pending', 'task_id': request['bridge']['identity']['task_id']},
            'previews': [], 'native_candidate_reopen': 'pass', 'content_sha256': content_sha,
            'bridge': {**request['bridge'], 'objects': names, 'receiver_reopen': 'pass',
                       'base_color_maps': observed['base_color_maps']}}


def bridge_datablocks(witness):
    objects = witness['objects']
    materials = witness['materials']
    return {'objects': sorted(objects),
            'meshes': sorted({obj['mesh']['name'] for obj in objects.values() if 'mesh' in obj}),
            'materials': sorted(materials),
            'images': sorted({image['name'] for mat in materials.values() for image in mat['images'].values()})}


def bridge_provenance(request):
    bridge = request['bridge']
    return {'identity': bridge['identity'], 'source': bridge['source'], 'received_sha256': bridge['received']['sha256'],
            'candidate_sha256': request['candidate']['sha256'], 'original_qualifications': bridge['original_qualifications']}


def verify_bridge_append(request, bpy, scene, names):
    """Compare the producer's native witness using Blender's actual source → local ID mapping."""
    witness = read_json(request['report']['file'])['verification']
    blocks = bridge_datablocks(witness)
    if set(names) != set(blocks) or any(sorted(names[k]) != source or len(set(names[k].values())) != len(source)
                                         for k, source in blocks.items()):
        raise Failure('CONFLICT', 'Studio Bridge ID map differs from the reviewed native inventory')
    expected = deepcopy({field: witness[field] for field in WITNESS_FIELDS})
    for row in expected['objects'].values():
        if row['parent'] is not None:
            row['parent'] = names['objects'][row['parent']]
        if 'mesh' in row:
            row['mesh']['name'] = names['meshes'][row['mesh']['name']]
            for slots in ('material_slots', 'mesh_materials'):
                row[slots] = [names['materials'][name] if name is not None else None for name in row[slots]]
    expected['objects'] = {names['objects'][k]: row for k, row in expected['objects'].items()}
    expected['materials'] = {names['materials'][k]: row for k, row in expected['materials'].items()}
    for mat in expected['materials'].values():
        for image in mat['images'].values():
            image['name'] = names['images'][image['name']]
    for image in expected['base_color_maps']:
        image['name'] = names['images'][image['name']]
        image['material'] = names['materials'][image['material']]
    expected['base_color_maps'].sort(key=lambda row: (row['material'], row['name']))
    observed = native_reader()._scene_state(bpy, scene, sorted(expected['objects']))
    for field in WITNESS_FIELDS:
        if expected[field] != observed[field]:
            raise Failure('VALIDATION_FAILED', f'Appended Studio Bridge {field} differs from the reviewed candidate')
    provenance = json.dumps(bridge_provenance(request), ensure_ascii=False, sort_keys=True)
    if scene.get('mw_bridge_provenance') != provenance:
        raise Failure('CONFLICT', 'Appended scene does not carry its original Studio Bridge identity and qualifications')
