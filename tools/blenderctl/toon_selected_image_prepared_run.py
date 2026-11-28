# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare selected numeric expectations from protected fixed export roles.

Caller owns CLI/binary authorization, the outer HeldFiles lifetime and eventual
parent publication. This module neither runs numerical work nor reads receipts.
"""
from dataclasses import dataclass, fields
import copy
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re

from protocol import Failure
import toon_image_prepared_run as legacy
from toon_selected_export_contract import normalize_request, EXPORT_HARD_LIMITS

_BACKEND = Path(legacy.__file__).resolve().parent.parent / 'toon_image_backend'
_METADATA_BYTES = 8 * 1024**2
_FILE_BYTES = 512 * 1024**2
_INPUT_BYTES = 4 * 1024**2
_COMPATIBILITY = dict(width_px=1, feather_px=0, visibility_step_px=1,
    ray_epsilon=1e-6, color=[0, 0, 0, 1], samples_per_axis=1, include_open=True)
_ARRAYS = frozenset(('co','edges','loop_vertex','loop_edge','face_start','face_total',
    'face_material','face_smooth','face_normal','edge_sharp','corner_normals',
    'triangles','triangle_polygon','matrix_world'))


def _need(value, message, code='CONFLICT'):
    if not value:
        raise Failure(code, message)


def _fields(value, names, message):
    _need(type(value) is dict and len(value) == len(names) and
        all(type(key) is str and key in names for key in value), message, 'INVALID_REQUEST')


def _uint(value, message):
    _need(type(value) is int and 0 <= value <= 2**63-1, message)
    return value


def _digest(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None, 'Native lowercase SHA256 required', 'INVALID_REQUEST')
    return value


def _native(value):
    """Bounded exact JSON values; preserve bool/int/float distinctions."""
    nodes = [0]
    def visit(item, depth):
        nodes[0] += 1
        _need(depth <= 64 and nodes[0] <= 262144, 'Declaration structure limit', 'RESOURCE_LIMIT')
        kind = type(item)
        if kind is dict:
            _need(nodes[0] + 2*len(item) <= 262144, 'Declaration node limit', 'RESOURCE_LIMIT')
            for key, child in item.items():
                _need(type(key) is str, 'Native JSON string keys required', 'INVALID_REQUEST')
                visit(key, depth+1)
                visit(child, depth+1)
        elif kind is list:
            _need(nodes[0] + len(item) <= 262144, 'Declaration node limit', 'RESOURCE_LIMIT')
            for child in item:
                visit(child, depth+1)
        elif kind is str:
            item.encode('utf-8', errors='strict')
        elif kind is float:
            _need(math.isfinite(item), 'Finite JSON values required', 'INVALID_REQUEST')
        else:
            _need(item is None or kind in (int, bool), 'Native JSON values required', 'INVALID_REQUEST')
    visit(value, 0)
    return value


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def _same(actual, expected, message):
    # JSON spelling distinguishes true/1, 1/1.0 and signed floating zero; all
    # callers supply parsed native JSON or already-validated native declarations.
    if type(expected) in (set, frozenset):
        _need(type(actual) in (set, frozenset) and all(type(value) is str for value in actual) and actual == expected, message)
    else:
        _need(_canonical(actual) == _canonical(expected), message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _descriptor(value, path, held, *, low=False, maximum=_METADATA_BYTES):
    key = 'sha256' if low else 'expected_sha256'
    _fields(value, ('file', key, 'bytes'), 'Exact fixed-role descriptor required')
    _need(type(value['file']) is str and value['file'] == str(path), 'Descriptor escaped fixed role')
    _digest(value[key])
    _uint(value['bytes'], 'Native descriptor extent required')
    return held.file(path, value[key], maximum=maximum, size=value['bytes'])


def _export_members(root, held):
    """Export contains a directory: do not pass it to HeldFiles.flat_names."""
    held.directory(root)
    expected = {'capture.json': False, 'resources.json': False, 'geometry': True}
    found = {}
    with os.scandir(root) as entries:
        for entry in entries:
            held.checkpoint()
            _need(len(found) < 3 and entry.name not in found, 'Export member count/alias differs')
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            _need(not attrs & 0x400, 'Export reparse member forbidden')
            found[entry.name] = bool(attrs & 0x10)
    _same(found, expected, 'Exact export root members/types required')


def _source_inputs(capture, normalized, held):
    wanted = [dict(file=str(legacy.local_path(normalized['file'])), expected_sha256=normalized['expected_sha256']),
        *[dict(file=str(legacy.local_path(row['file'])), expected_sha256=row['expected_sha256']) for row in normalized['resources']]]
    _need(type(capture.get('resources')) is list, 'Capture resource list required')
    actual = [capture.get('source'), *capture['resources']]
    _need(len(actual) == len(wanted), 'Capture input count differs')
    _same(capture.get('verified_inputs_after'), actual, 'Capture before/after declarations differ')
    total = 0
    for item, expected in zip(actual, wanted):
        _fields(item, ('file','expected_sha256','bytes','volume','file_index','links','attributes'), 'Exact saved input metadata required')
        _same({key: item[key] for key in expected}, expected, 'Original input declaration differs')
        for key in ('bytes','volume','file_index','links','attributes'):
            _uint(item[key], 'Native input metadata: '+key)
        row = held.file(item['file'], item['expected_sha256'], maximum=_FILE_BYTES, size=item['bytes'])
        info = row['guard']._information()
        _same([item['volume'], item['file_index']], list(row['identity']), 'Input handle identity differs')
        _same(item['attributes'], int(info.attributes), 'Input attributes differ')
        _need(item['links'] == int(info.links) == 1, 'Single-link input required')
        total += row['bytes']
    return wanted, total


def _audit(document, wanted, total, context, runtime, implementation):
    _digest(implementation)
    fixed = dict(version='1.0', adapter='TYPED_FILES_V1', closure='closed', issues=[],
        source_saved=False, source=wanted[0], blender_build=runtime['blender_build'],
        implementation_sha256=implementation,
        profile=dict(closure=dict(adapter='TYPED_FILES_V1', max_files=101, max_depth=8),
            timeline=dict(scene=context['scene'], start=context['frame'], end=context['frame'], step=1)))
    for key, expected in fixed.items():
        _same(document.get(key), expected, 'Typed audit '+key+' differs')
    _uint(document.get('file_count'), 'Native audit file count required')
    _uint(document.get('bytes'), 'Native audit bytes required')
    _need(document['file_count'] == len(wanted) and document['bytes'] == total, 'Audit held-byte/count closure differs')
    entries = document.get('documents')
    _need(type(entries) is list and len(entries) == len(wanted), 'Audit document list differs')
    observed = {}
    for entry in entries:
        _fields(entry, ('file','expected_sha256'), 'Exact audit input descriptor required')
        key = ntpath.normcase(str(legacy.local_path(entry['file'])))
        _need(key not in observed, 'Duplicate audit document')
        observed[key] = _digest(entry['expected_sha256'])
    _same(observed, {ntpath.normcase(row['file']): row['expected_sha256'] for row in wanted}, 'Audit document set differs')
    rejected = {'point_cache','fluid_cache','rigidbody_cache','geometry_nodes_cache','geometry_nodes_bake',
                'cache_file','volume','declared_cache','declared_plugin'}
    resources = document.get('resources')
    _need(type(resources) is list and len(resources) <= 4096, 'Bounded static resource list required')
    for resource in resources:
        _need(type(resource) is dict and type(resource.get('kind')) is str and resource['kind'] not in rejected and
            type(resource.get('requirement')) is str and resource['requirement'] != 'unknown' and
            resource.get('source') != 'SEQUENCE', 'Dynamic/unknown dependency outside static scope')
        _same(resource.get('frame_mapping', []), [], 'Static resources cannot have a frame mapping')


@dataclass(frozen=True, slots=True)
class PreparedSelectedRun:
    request_path: str
    request_sha256: str
    expected_path: str
    expected_sha256: str
    expected_run: object
    host_context_sha256: str
    bundle_path: str


def prepare_exported_selected_run(*, run_root, export_request, numeric_recipe,
        capture_sha256, dependency_audit, host_context, expected_runtime,
        expected_backend_sources, expected_dependency_implementation, held):
    """Seal independent expectations before numeric. Partial files never authorize it."""
    _need(isinstance(held, legacy.HeldFiles), 'Existing host HeldFiles required', 'INVALID_REQUEST')
    for declaration in (export_request, numeric_recipe, host_context, expected_runtime, expected_backend_sources):
        _native(declaration)
    export_request, numeric_recipe, host_context, expected_runtime, expected_backend_sources = (
        copy.deepcopy(value) for value in (export_request,numeric_recipe,host_context,expected_runtime,expected_backend_sources))
    _fields(numeric_recipe, ('selector','visible_enabled','hidden_enabled','chaining','style','budgets'), 'Exact internal numeric recipe required')
    _need(type(numeric_recipe['visible_enabled']) is bool and type(numeric_recipe['hidden_enabled']) is bool,
        'Literal channel booleans required', 'INVALID_REQUEST')
    normalized = legacy.plain(normalize_request(export_request))
    context = normalized['context']
    root = legacy.local_path(str(run_root))
    bundle = root / 'bundle'
    _need(not os.path.lexists(bundle), 'Numeric bundle exists before preparation')
    held.directory(root)
    _fields(host_context, ('schema','run_root','nonce','export_request_sha256','numeric_recipe_sha256'), 'Exact host context required')
    _same(host_context['schema'], 'toon-selected-image-host-context.v1', 'Host context schema differs')
    _same(host_context['run_root'], str(root), 'Host context root differs')
    _need(type(host_context['nonce']) is str and re.fullmatch('[0-9a-f]{32}', host_context['nonce']) is not None, 'Host nonce differs')
    _same(_digest(host_context['export_request_sha256']), _sha(_canonical(export_request)), 'Original export request SHA differs')
    _same(_digest(host_context['numeric_recipe_sha256']), _sha(_canonical(numeric_recipe)), 'Original numeric recipe SHA differs')
    _fields(expected_runtime, ('python','numpy','blender','blender_build'), 'Complete independent target runtime required')
    _need(all(type(value) is str and 0 < len(value) <= 4096 for value in expected_runtime.values()), 'Native runtime strings required')

    export = root / 'export'
    geometry = export / 'geometry'
    _export_members(export, held)
    capture = held.document(export / 'capture.json', _digest(capture_sha256), _METADATA_BYTES)
    _need(type(capture) is dict, 'Capture object required')
    for key, expected in dict(schema='toon-selected-image-saved-capture.v1', source_mode='SAVED_DISK_V1',
            declared_request=normalized, inputs_unchanged=True, source_saved=False,
            numerical_producer_run=False, parent_accepted=False, native_source_authenticated=False,
            runtime=expected_runtime).items():
        _same(capture.get(key), expected, 'Capture '+key+' differs')
    for key in ('scene','view_layer','frame','camera_name','occluder_object_names','member_object_names'):
        capture_key = 'object_names' if key == 'occluder_object_names' else key
        _same(capture.get(capture_key), context[key], 'Capture context '+key+' differs')
    _same(capture.get('object_ids'), context['occluder_object_names'], 'Capture object order differs')
    _same(capture.get('member_object_ids'), context['member_object_names'], 'Capture members differ')
    _same(capture.get('camera_type'), 'ORTHO', 'Capture camera type differs')
    wanted, source_total = _source_inputs(capture, normalized, held)
    audit_row = _descriptor(dependency_audit, export / 'resources.json', held)
    audit = held.document(export / 'resources.json', audit_row['sha256'], _METADATA_BYTES)
    _audit(audit, wanted, source_total, context, expected_runtime, expected_dependency_implementation)

    _descriptor(capture.get('scene_export'), geometry / 'EXPORT.json', held)
    exported = held.document(geometry / 'EXPORT.json', capture['scene_export']['expected_sha256'], _METADATA_BYTES)
    _same(exported, capture.get('scene_export_record'), 'EXPORT differs from capture record')
    for key, expected in dict(schema='toon-selected-scene-export.v1', scene=context['scene'], view_layer=context['view_layer'],
            frame=context['frame'], object_ids=context['occluder_object_names'], source_saved=False,
            numerical_producer_run=False, parent_accepted=False, native_source_authenticated=False,
            concurrent_writer_safe=False, runtime={key:expected_runtime[key] for key in ('blender','blender_build')}).items():
        _same(exported.get(key), expected, 'EXPORT '+key+' differs')
    _same(exported.get('depsgraph_mode'), capture.get('graph_mode'), 'Evaluated graph mode differs')
    export_limits = dict(EXPORT_HARD_LIMITS)
    export_limits.update(normalized['export_limits'])
    _same(exported.get('limits'), export_limits, 'Original export limits differ')
    _fields(exported.get('counts'), ('max_vertices','max_edges','max_triangles','max_loops','max_polygons','max_array_bytes'),
        'Complete exporter count metadata required')
    for key, count_value in exported['counts'].items():
        _uint(count_value, 'Native exporter count required')
        _need(count_value <= export_limits[key], 'Exporter declared count budget', 'RESOURCE_LIMIT')
    _uint(exported.get('charged_input_bytes'), 'Native charged input bytes required')
    _need(exported['charged_input_bytes'] <= export_limits['max_input_bytes'], 'Exporter declared input byte budget', 'RESOURCE_LIMIT')
    input_path = geometry / 'toon-image-input.json'
    input_row = _descriptor(capture.get('input'), input_path, held, maximum=_INPUT_BYTES)
    _descriptor(exported.get('input'), input_path, held, low=True, maximum=_INPUT_BYTES)
    _same(capture['input']['expected_sha256'], exported['input']['sha256'], 'Input digest declarations differ')
    input_doc = held.document(input_path, input_row['sha256'], _INPUT_BYTES)
    _fields(input_doc, ('schema','frame','objects','camera','settings'), 'Exact selected input fields required')
    _same(input_doc['schema'], 'toon-image-input.v1', 'Input schema differs')
    _same(input_doc['frame'], context['frame'], 'Input frame differs')
    _same(input_doc['settings'], _COMPATIBILITY, 'Fixed loader compatibility settings differ')
    _same(input_doc['camera'], exported.get('camera'), 'Input/EXPORT camera differs')
    _same(input_doc['camera'], capture.get('camera'), 'Input/capture camera differs')
    camera = input_doc['camera']
    _need(type(camera) is dict, 'Camera object required')
    _fields(camera, ('kind','width','height','world','projection','clip_start','clip_end'), 'Exact camera fields required')
    for key in ('width','height'):
        _uint(camera[key], 'Native image dimension required')
        _same(camera[key], capture.get(key), 'Capture dimensions differ')
        _need(0 < camera[key] <= export_limits['max_dimension'], 'Export dimension budget', 'RESOURCE_LIMIT')
    _need(camera['width']*camera['height'] <= export_limits['max_pixels'], 'Export pixel budget', 'RESOURCE_LIMIT')
    objects, captures, entries = exported.get('objects'), capture.get('npz'), input_doc['objects']
    count = len(context['occluder_object_names'])
    _need(type(objects) is list and type(captures) is list and type(entries) is list and
        len(objects) == len(captures) == len(entries) == count, 'Complete ordered object declarations required')
    expected_members = {'EXPORT.json','toon-image-input.json'}
    npz_rows = []
    for ordinal, name in enumerate(context['occluder_object_names']):
        low, high, entry = objects[ordinal], captures[ordinal], entries[ordinal]
        _fields(low, ('object_id','file','arrays','evaluated_matrix_world','material_culling'), 'Exact EXPORT object required')
        _fields(high, ('object_id','file','arrays','evaluated_matrix_world','material_culling'), 'Exact capture object required')
        _fields(entry, ('id','name','file','sha256','matrix_world','material_culling'), 'Exact input object required')
        _same([low['object_id'],high['object_id'],entry['id'],entry['name']], [name]*4, 'Object identity/order differs')
        path = geometry / ('export-%06d.npz' % ordinal)
        expected_members.add(path.name)
        low_row = _descriptor(low['file'], path, held, low=True, maximum=_FILE_BYTES)
        _descriptor(high['file'], path, held, maximum=_FILE_BYTES)
        _same(entry['file'], str(path), 'Input NPZ role escaped')
        _same(entry['sha256'], low_row['sha256'], 'Input NPZ digest differs')
        for key, input_key in (('arrays',None), ('evaluated_matrix_world','matrix_world'), ('material_culling','material_culling')):
            _same(low[key], high[key], 'Capture/EXPORT object '+key+' differs')
            if input_key is not None:
                _same(entry[input_key], low[key], 'Input object '+key+' differs')
        _need(type(low['arrays']) is dict and set(low['arrays']) == _ARRAYS, 'Complete fourteen array declarations required')
        for declaration in low['arrays'].values():
            _fields(declaration, ('dtype','shape','payload_sha256'), 'Exact array declaration required')
            _digest(declaration['payload_sha256'])
            _need(type(declaration['dtype']) is str and type(declaration['shape']) is list and len(declaration['shape']) in (1,2),
                'Native array dtype/shape declarations required')
            for dimension in declaration['shape']:
                _uint(dimension, 'Native array shape dimension required')
        npz_rows.append(low_row)
    _same(held.flat_names(geometry), expected_members, 'Exact geometry member set required')

    _need(type(expected_backend_sources) is dict and 0 < len(expected_backend_sources) <= 4096, 'Independent backend source inventory required')
    for name, pin in expected_backend_sources.items():
        _need(type(name) is str and name.endswith('.py') and '\\' not in name and ':' not in name and
            all(part not in ('','.','..') for part in name.split('/')), 'Canonical relative backend source required')
        _digest(pin)
    product = _BACKEND
    names = held.source_names(product)
    _same(names, sorted(expected_backend_sources), 'Backend member inventory differs')
    code_pins = {}
    for name in names:
        path = product / name
        row = held.file(path, expected_backend_sources[name], maximum=_METADATA_BYTES)
        code_pins[str(path)] = dict(bytes=row['bytes'], sha256=row['sha256'])
    # Nothing below is imported until every approved backend source is held.
    from toon_image_backend import selected_image_runner as runner, mesh_source_core as mesh
    from toon_image_backend import chain_image_core as image, selected_chain_planner as planner
    from toon_image_backend import selected_chain_image_receipt_reader as reader, selected_declarations
    from toon_image_backend import chain_phase, phase_support
    import numpy as np
    for module, name in ((runner,'selected_image_runner.py'),(mesh,'mesh_source_core.py'),(image,'chain_image_core.py'),
            (planner,'selected_chain_planner.py'),(reader,'selected_chain_image_receipt_reader.py'),(selected_declarations,'selected_declarations.py'),
            (chain_phase,'chain_phase.py'),(phase_support,'phase_support.py')):
        _need(Path(module.__file__).resolve() == product / name, 'Actual imported backend module path differs')
    _same(image.code_pins(runner.__file__, (mesh.__file__, planner.__file__)), code_pins, 'Actual producer code closure differs')
    request = dict(schema=runner.SCHEMA, input=dict(input_path=str(input_path), input_sha256=input_row['sha256']),
        selector=numeric_recipe['selector'], policy=dict(model=runner.MODEL, member_object_ids=context['member_object_names'],
            visible_enabled=numeric_recipe['visible_enabled'], hidden_enabled=numeric_recipe['hidden_enabled']),
        chaining=numeric_recipe['chaining'], style=numeric_recipe['style'], budgets=numeric_recipe['budgets'])
    request_data = image.canonical(request)
    _need(len(request_data) <= image.MAX_REQUEST_BYTES, 'Request byte budget', 'RESOURCE_LIMIT')
    parsed = image.parse_request(request_data)
    _same(parsed, request, 'Serialized request differs')
    policy, style, binding, phase, render, total, planning = runner.validate_request(request)
    selected_declarations.policy_style(policy, style)
    effective_phase, _ = chain_phase.normalize_limits(phase)
    arithmetic = phase_support.Arithmetic(effective_phase['max_bits'])
    for key in ('period_px','phase_px','duty'):
        arithmetic.parse(style['chain_dash'][key], '$.dash.'+key)
    held.checkpoint()
    scene = mesh.PreparedMeshScene(str(input_path), input_row['sha256'], [str(root)], limits=binding)
    held.checkpoint()
    locks = scene.input_files
    _need(len(locks) == 1+count, 'Complete scene input locks required')
    _same({key: locks[0][key] for key in ('path','sha256','bytes')},
        dict(path=str(input_path), sha256=input_row['sha256'], bytes=input_row['bytes']), 'Scene input JSON lock differs')
    counts = dict(max_vertices=0,max_edges=0,max_triangles=0,max_loops=0,max_polygons=0,max_array_bytes=0)
    charge = input_row['bytes']
    for ordinal, (lock, held_row, obj) in enumerate(zip(locks[1:], npz_rows, objects)):
        _same({key:lock[key] for key in ('path','sha256','bytes')},
            dict(path=held_row['file'], sha256=held_row['sha256'], bytes=held_row['bytes']), 'Scene NPZ lock differs')
        headers = lock['members']
        _same(set(headers), _ARRAYS, 'Scene NPZ member set differs')
        for key, header in headers.items():
            declaration = obj['arrays'][key]
            _same(header['shape'], declaration['shape'], 'NPZ header shape differs')
            _same(header['dtype'], declaration['dtype'], 'NPZ header dtype differs')
            counts['max_array_bytes'] += math.prod(header['shape']) * np.dtype(header['dtype']).itemsize
        for key, name in (('co','max_vertices'),('edges','max_edges'),('triangles','max_triangles'),('loop_vertex','max_loops'),('face_start','max_polygons')):
            counts[name] += headers[key]['shape'][0]
        charge += lock['bytes'] + sum(header['bytes'] for header in headers.values())
    _same(counts, exported.get('counts'), 'Export count/array-byte declarations differ')
    _same(charge, exported.get('charged_input_bytes'), 'Export charged input bytes differ')
    _need(charge <= export_limits['max_input_bytes'] and all(value <= export_limits[key] for key,value in counts.items()),
        'Export input/count budget', 'RESOURCE_LIMIT')
    _same(scene.screen_binding['width'], camera['width'], 'Prepared image width differs')
    _same(scene.screen_binding['height'], camera['height'], 'Prepared image height differs')
    _need(camera['width']*camera['height'] <= render['max_pixels'], 'Numeric pixel budget', 'RESOURCE_LIMIT')
    request_path = root / 'backend-request.json'
    sealed_context = dict(schema='toon-selected-image-numeric-context.v1', parent=host_context,
        export_request_sha256=host_context['export_request_sha256'], numeric_recipe_sha256=host_context['numeric_recipe_sha256'],
        capture_sha256=capture_sha256, dependency_audit_sha256=audit_row['sha256'],
        dependency_implementation_sha256=expected_dependency_implementation,
        scene_export_sha256=capture['scene_export']['expected_sha256'], input_sha256=input_row['sha256'],
        backend_request_sha256=_sha(request_data), runtime=expected_runtime, backend_sources=code_pins)
    context_data = _canonical(sealed_context)
    context_sha = _sha(context_data)
    values = dict(host_context_sha256=context_sha, request_path=str(request_path), request_sha256=_sha(request_data),
        request_bytes=len(request_data), canonical_request_utf8=request_data, source_input_sha256=input_row['sha256'],
        image_shape=(camera['height'],camera['width'],4), binding=scene.binding, screen_binding=scene.screen_binding,
        camera_document=scene.camera_document, projection=scene.projection,
        input_locks=[dict(path=str(request_path),sha256=_sha(request_data),bytes=len(request_data)),*locks], code_pins=code_pins,
        runtime={key:expected_runtime[key] for key in ('python','numpy')}, policy=policy, style=style,
        effective_budgets=dict(binding=binding,phase=phase,render=render,total=total,planner=planning))
    kwargs = {key: (value if key in ('host_context_sha256','request_path','request_sha256','request_bytes',
        'canonical_request_utf8','source_input_sha256','image_shape') else reader.freeze_declaration(value)) for key,value in values.items()}
    expected = reader.ExpectedSelectedRun(**kwargs)
    serialized = {}
    for field in fields(reader.ExpectedSelectedRun):
        value = getattr(expected, field.name)
        serialized[field.name] = value.decode('utf-8') if field.name == 'canonical_request_utf8' else reader.thaw(value, held.checkpoint)
    expected_data = _canonical(serialized)
    _need(len(context_data) <= _METADATA_BYTES and len(expected_data) <= _METADATA_BYTES, 'Prepared metadata budget', 'RESOURCE_LIMIT')
    del scene
    expected_path = root / 'EXPECTED.json'
    held.checkpoint()
    for path, data in ((root / 'NUMERIC-CONTEXT.json', context_data), (request_path, request_data), (expected_path, expected_data)):
        with path.open('xb') as stream:
            stream.write(data)
        held.file(path, _sha(data), maximum=_METADATA_BYTES, size=len(data))
    _same(held.document(expected_path, _sha(expected_data), _METADATA_BYTES), serialized, 'Held Expected encoding differs')
    held.recheck()
    _export_members(export, held)
    _same(held.flat_names(geometry), expected_members, 'Geometry membership changed during prepare')
    _same(held.source_names(product), names, 'Backend membership changed during prepare')
    _need(not os.path.lexists(bundle), 'Numeric bundle appeared during preparation')
    return PreparedSelectedRun(str(request_path), _sha(request_data), str(expected_path), _sha(expected_data), expected, context_sha, str(bundle))
