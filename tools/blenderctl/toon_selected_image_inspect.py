# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect a pinned successful selected parent using historical fixed roles.

Current source/resource/binary/producer paths are declarations, never inputs.
The caller's ExitStack owns all evidence guards and the formal selected lease.
"""
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import math
import ntpath
import os
import re

from toon_image_inspect import _Held, _Check, _json, _local, _digest, _need
from toon_image_public import canonical, implementation_sha256, plain
from toon_image_contract import ImageContractError
from protocol import Failure
from toon_selected_image_contract import normalize_params
from toon_selected_export_contract import EXPORT_HARD_LIMITS
from toon_selected_image_prepared_run import _audit, _ARRAYS, _COMPATIBILITY
from toon_selected_image_evidence import (split_params, expected_document,
    expected_from_document, cold_document)
from selected_bundle_lease import SelectedWindowsBundleLease, SelectedLeaseLimits
from toon_image_backend import selected_chain_image_receipt_reader as reader
from toon_image_backend import selected_declarations as declarations

PROFILE = 'selected-mesh-image.v1'
_CAP = 8 * 1024**2
_FILE_CAP = 512 * 1024**2


def _fields(value, names):
    _need(type(value) is dict and set(value) == set(names) and
          all(type(key) is str for key in value), 'Exact native document fields required', 'VALIDATION_FAILED')


def _required(value, names):
    _need(type(value) is dict and set(names) <= set(value),
          'Native metadata object with required fields expected', 'VALIDATION_FAILED')


def _same(actual, expected, message):
    _need(canonical(actual) == canonical(expected), message)


def _uint(value, name):
    _need(type(value) is int and 0 <= value < 2**63, 'Native unsigned metadata required: '+name)
    return value


def _fixed(held, value, path, *, low=False, maximum=_CAP):
    key = 'sha256' if low else 'expected_sha256'
    _fields(value, ('file', key, 'bytes'))
    _need(type(value['file']) is str and value['file'] == str(path), 'Historical descriptor left fixed role')
    size = _uint(value['bytes'], 'descriptor bytes')
    return held.file(path, _digest(value[key]), maximum=maximum, size=size)


def _export_members(held, root, check):
    held.directory(root)
    found = {}
    with os.scandir(root) as entries:
        for item in entries:
            check()
            _need(len(found) < 3 and item.name not in found, 'Historical export root member count/alias differs')
            attributes = item.stat(follow_symlinks=False).st_file_attributes
            _need(not attributes & 0x400, 'Historical export root contains a reparse member')
            found[item.name] = bool(attributes & 0x10)
    _same(found, {'capture.json':False, 'resources.json':False, 'geometry':True},
          'Historical export root members/types differ')


def _historical_sources(capture, export_request):
    wanted = [dict(file=export_request['file'], expected_sha256=export_request['expected_sha256']),
              *export_request['resources']]
    _need(type(capture.get('resources')) is list, 'Historical capture resources required')
    captured = [capture.get('source'), *capture['resources']]
    _need(len(captured) == len(wanted), 'Historical source/resource count differs')
    _same(capture.get('verified_inputs_after'), captured, 'Historical before/after input declarations differ')
    total = 0
    for row, expected in zip(captured, wanted):
        _fields(row, ('file','expected_sha256','bytes','volume','file_index','links','attributes'))
        _same({key:row[key] for key in ('file','expected_sha256')}, expected,
              'Historical source/resource declaration differs')
        for key in ('bytes','volume','file_index','links','attributes'):
            _uint(row[key], key)
        _need(row['bytes'] <= _FILE_CAP and row['links'] == 1, 'Historical source byte/link declaration differs')
        total += row['bytes']
    # Deliberately no file/resolve/stat/guard of any captured source path.
    return wanted, total


def _launch_sources(identity, implementation):
    code = identity.get('code')
    _need(type(identity) is dict and identity.get('schema') == 'toon-image-launch.v1' and
          type(code) is dict and code.get('schema') == 'toon-image-code.v1', 'Historical launch identity differs')
    _need(implementation_sha256(code) == implementation, 'Historical launch implementation differs')
    sources = code.get('sources')
    _need(type(sources) is dict and 0 < len(sources) <= 4096, 'Bounded original code inventory required')
    backend, cli = {}, {}
    for name, digest in sources.items():
        _need(type(name) is str and name.endswith('.py') and '\\' not in name and ':' not in name and
              all(part not in ('','.','..') for part in name.split('/')), 'Canonical original source inventory required')
        _digest(digest)
        if name.startswith('toon_image_backend/'):
            backend[name[len('toon_image_backend/'):]] = digest
        else:
            _need(name.startswith('blenderctl/') and name.count('/') == 1, 'Unknown original source inventory root')
            cli[name[len('blenderctl/'):]] = digest
    _need(bool(backend) and bool(cli), 'Complete original CLI/backend inventories required')
    typed = hashlib.sha256(json.dumps(cli, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return backend, typed


def _code_pins(pins, backend):
    _need(type(pins) is dict and len(pins) == len(backend), 'Historical complete backend pins differ')
    matched, roots = {}, set()
    for path, row in pins.items():
        _local(path)  # lexical declaration validation only; never opens code.
        _fields(row, ('bytes','sha256'))
        _uint(row['bytes'], 'source extent'); _digest(row['sha256'])
        normalized = path.replace('\\','/')
        names = [name for name in backend if normalized.endswith('/toon_image_backend/'+name)]
        _need(len(names) == 1 and names[0] not in matched, 'Historical backend code path relation differs')
        name = names[0]
        _need(row['sha256'] == backend[name], 'Historical backend code digest differs')
        matched[name] = row
        roots.add(ntpath.normcase(normalized[:-len('/toon_image_backend/'+name)]))
    _need(set(matched) == set(backend) and len(roots) == 1, 'Historical backend root/inventory differs')


def _camera(input_camera, expected):
    _fields(input_camera, ('kind','width','height','world','projection','clip_start','clip_end'))
    _need(input_camera['kind'] == 'ORTHO', 'Historical ORTHO camera required')
    exact = dict(schema='toon-intersection-camera.exact.v1', width=input_camera['width'],
        height=input_camera['height'], margin_exact=[0,1])
    def rational(value):
        _need(type(value) in (int,float), 'Native historical camera number required', 'VALIDATION_FAILED')
        try:
            converted = float(value)
        except (ValueError, OverflowError) as error:
            raise Failure('VALIDATION_FAILED', 'Historical camera number exceeds binary64') from error
        _need(math.isfinite(converted), 'Finite historical camera number required', 'VALIDATION_FAILED')
        f = Fraction.from_float(converted)
        return [f.numerator, f.denominator]
    for key in ('world','projection'):
        matrix = input_camera[key]
        _need(type(matrix) is list and len(matrix) == 4 and
              all(type(row) is list and len(row) == 4 for row in matrix), 'Historical camera matrix shape differs')
        exact[key+'_exact'] = [[rational(value) for value in row] for row in matrix]
    exact['near_exact'] = rational(input_camera['clip_start'])
    exact['far_exact'] = rational(input_camera['clip_end'])
    _same(expected['camera_document'], exact, 'Historical Expected exact camera differs from pinned input')
    camera_id = hashlib.sha256(json.dumps(exact, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    _same(expected['screen_binding']['camera_id'], camera_id, 'Historical camera identity differs')
    _same(expected['image_shape'], [input_camera['height'],input_camera['width'],4], 'Historical image shape differs')


def _geometry(held, root, capture, export_request, expected, check):
    export, geometry = root/'export', root/'export/geometry'
    context = export_request['context']
    _export_members(held, export, check)
    _fixed(held, capture['scene_export'], geometry/'EXPORT.json')
    low = held.document(geometry/'EXPORT.json', capture['scene_export']['expected_sha256'])
    _required(low, ('input','objects','counts','charged_input_bytes'))
    _same(low, capture.get('scene_export_record'), 'Historical EXPORT/capture record differs')
    runtime = capture['runtime']
    for key, value in dict(schema='toon-selected-scene-export.v1', scene=context['scene'],
            view_layer=context['view_layer'], frame=context['frame'], object_ids=context['occluder_object_names'],
            source_saved=False, numerical_producer_run=False, parent_accepted=False,
            native_source_authenticated=False, concurrent_writer_safe=False,
            runtime={key:runtime[key] for key in ('blender','blender_build')}).items():
        _same(low.get(key), value, 'Historical EXPORT '+key+' differs')
    _same(low.get('depsgraph_mode'), capture.get('graph_mode'), 'Historical graph mode differs')
    limits = dict(EXPORT_HARD_LIMITS); limits.update(export_request['export_limits'])
    _same(low.get('limits'), limits, 'Historical export limits differ')
    _fields(low.get('counts'), ('max_vertices','max_edges','max_triangles','max_loops','max_polygons','max_array_bytes'))
    for key, value in low['counts'].items():
        _need(_uint(value, key) <= limits[key], 'Historical export count budget differs', 'RESOURCE_LIMIT')
    _need(_uint(low.get('charged_input_bytes'), 'charged input') <= limits['max_input_bytes'],
          'Historical export charged budget exceeded', 'RESOURCE_LIMIT')
    input_path = geometry/'toon-image-input.json'
    row = _fixed(held, capture['input'], input_path, maximum=4*1024**2)
    _fixed(held, low['input'], input_path, low=True, maximum=4*1024**2)
    document = held.document(input_path, row['sha256'])
    _fields(document, ('schema','frame','objects','camera','settings'))
    _same(document['schema'], 'toon-image-input.v1', 'Historical input schema differs')
    _same(document['frame'], context['frame'], 'Historical input frame differs')
    _same(document['settings'], _COMPATIBILITY, 'Historical input compatibility settings differ')
    _same(document['camera'], low.get('camera'), 'Historical input/EXPORT camera differs')
    _same(document['camera'], capture.get('camera'), 'Historical input/capture camera differs')
    camera = document['camera']; _camera(camera, expected)
    for key in ('width','height'):
        value = _uint(camera[key], 'image dimension')
        _need(0 < value <= limits['max_dimension'], 'Historical image dimension budget', 'RESOURCE_LIMIT')
        _same(camera[key], capture.get(key), 'Historical capture dimension differs')
    _need(camera['width']*camera['height'] <= limits['max_pixels'], 'Historical pixel budget', 'RESOURCE_LIMIT')
    objects, high, entries = low.get('objects'), capture.get('npz'), document['objects']
    size = len(context['occluder_object_names'])
    _need(type(objects) is list and type(high) is list and type(entries) is list and
          len(objects) == len(high) == len(entries) == size, 'Historical ordered object count differs')
    locks = expected['input_locks']
    _need(len(locks) == size+2, 'Complete historical request/JSON/NPZ lock set required')
    _same(locks[1], dict(path=str(input_path),sha256=row['sha256'],bytes=row['bytes']), 'Historical JSON lock differs')
    _same(expected['source_input_sha256'], row['sha256'], 'Historical source input digest differs')
    members = {'EXPORT.json','toon-image-input.json'}
    counts = dict.fromkeys(low['counts'], 0); charge = row['bytes']
    for ordinal, name in enumerate(context['occluder_object_names']):
        check()
        obj, captured, entry, lock = objects[ordinal], high[ordinal], entries[ordinal], locks[ordinal+2]
        for objrow in (obj,captured):
            _fields(objrow, ('object_id','file','arrays','evaluated_matrix_world','material_culling'))
        _fields(entry, ('id','name','file','sha256','matrix_world','material_culling'))
        _same([obj['object_id'],captured['object_id'],entry['id'],entry['name']], [name]*4, 'Historical object identity/order differs')
        path = geometry/('export-%06d.npz'%ordinal); members.add(path.name)
        actual = _fixed(held, obj['file'], path, low=True, maximum=_FILE_CAP)
        _fixed(held, captured['file'], path, maximum=_FILE_CAP)
        _same(entry['file'], str(path), 'Historical input NPZ fixed role differs')
        _same(entry['sha256'], actual['sha256'], 'Historical input NPZ digest differs')
        for key, input_key in (('arrays',None),('evaluated_matrix_world','matrix_world'),('material_culling','material_culling')):
            _same(obj[key], captured[key], 'Historical capture/EXPORT object metadata differs')
            if input_key is not None:
                _same(entry[input_key], obj[key], 'Historical input object metadata differs')
        _same({key:lock[key] for key in ('path','sha256','bytes')},
              dict(path=str(path),sha256=actual['sha256'],bytes=actual['bytes']), 'Historical NPZ lock differs')
        headers = lock.get('members'); arrays = obj['arrays']
        _need(type(headers) is dict and type(arrays) is dict and set(headers) == set(arrays) == _ARRAYS,
              'Historical complete fourteen NPZ header declarations required')
        _same(lock.get('extra_members'), ['matrix_world'], 'Historical NPZ extra-member declaration differs')
        for key, declaration in arrays.items():
            _fields(declaration, ('dtype','shape','payload_sha256')); _digest(declaration['payload_sha256'])
            header = headers[key]
            _same(header['dtype'], declaration['dtype'], 'Historical NPZ dtype declaration differs')
            _same(header['shape'], declaration['shape'], 'Historical NPZ shape declaration differs')
            _need(type(declaration['shape']) is list and len(declaration['shape']) in (1,2), 'Historical array rank differs')
            for dimension in declaration['shape']: _uint(dimension, 'array dimension')
            dtype = re.fullmatch(r'[<>=|]([biuf])([1-9][0-9]*)', declaration['dtype'])
            _need(dtype is not None, 'Historical canonical numeric dtype required')
            itemsize = int(dtype.group(2))
            _need(itemsize <= 16 and (dtype.group(1) != 'b' or itemsize == 1), 'Historical numeric dtype extent differs')
            body = math.prod(declaration['shape'])*itemsize
            _need(body <= header['bytes'], 'Historical NPZ member extent smaller than payload')
            counts['max_array_bytes'] += body
            charge += header['bytes']
        for key, count_key in (('co','max_vertices'),('edges','max_edges'),('triangles','max_triangles'),
                               ('loop_vertex','max_loops'),('face_start','max_polygons')):
            counts[count_key] += headers[key]['shape'][0]
        charge += actual['bytes']
        _need(charge <= limits['max_input_bytes'], 'Historical aggregate input byte budget', 'RESOURCE_LIMIT')
    _same(counts, low['counts'], 'Historical NPZ header/count accounting differs')
    _same(charge, low['charged_input_bytes'], 'Historical stored+uncompressed input charge differs')
    _need(held.flat(geometry) == members, 'Historical geometry complete member set differs')
    return geometry, members


def inspect_parent(params, inspector_job, *, stack, checkpoint, expected_implementation):
    _fields(params, ('parent_result','implementation_sha256'))
    _fields(params['parent_result'], ('file','expected_sha256'))
    implementation = _digest(expected_implementation)
    _need(_digest(params['implementation_sha256']) == implementation, 'Inspect implementation differs')
    result_path = _local(params['parent_result']['file'])
    result_sha = _digest(params['parent_result']['expected_sha256']); job = result_path.parent
    _need(result_path.name == 'result.json' and re.fullmatch('[0-9a-f]{32}', job.name) is not None,
          'Pinned parent result must be a fixed 32hex job/result.json', 'INVALID_REQUEST')
    own = _local(str(inspector_job))
    _need(own.is_dir() and not own.is_relative_to(job) and not job.is_relative_to(own),
          'Inspector output overlaps historical parent job', 'INVALID_REQUEST')
    check = _Check(checkpoint); held = _Held(stack, check)
    try:
        return _inspect(params, own, job, result_path, result_sha, held, check, stack, implementation)
    except BaseException:
        if check.error is not None: raise check.error
        raise


def _inspect(params, own, job, result_path, result_sha, held, check, stack, implementation):
    check(); result = held.document(result_path, result_sha)
    _fields(result, ('schema_version','cli_version','command','job_id','ok','data','error','artifacts'))
    _need(result['schema_version'] == '1.0' and result['command'] == 'toon-image.run' and
          result['job_id'] == job.name and result['ok'] is True and result['error'] is None, 'Original selected parent is not successful')
    root, bundle = job/'image-run', job/'image-run/bundle'
    _same(result['artifacts'], dict(job_directory=str(job),image_acceptance=str(job/'image-acceptance.json'),
          image_run_directory=str(root)), 'Original result fixed artifacts differ')
    data = result['data']
    _fields(data, ('status','profile','parent_accepted','parent_job_id','source_unchanged',
        'source_mode','quality_accepted','acceptance','result_root','cold_report','qualification'))
    _need(type(data) is dict and data.get('status') == 'passed' and data.get('parent_accepted') is True and
          data.get('parent_job_id') == job.name and data.get('source_mode') == 'SAVED_DISK_V1' and
          data.get('profile') == PROFILE and data.get('source_unchanged') is True and
          data.get('quality_accepted') is False and data.get('result_root') == str(bundle), 'Original result has no selected parent acceptance')
    status = held.document(job/'status.json')
    _required(status, ('job_id','state'))
    _need(status.get('job_id') == job.name and status.get('state') == 'succeeded' and
          type(status.get('exit_code')) is int and status['exit_code'] == 0, 'Original parent terminal differs')
    acceptance = held.descriptor(data['acceptance'], job/'image-acceptance.json', True)
    _fields(acceptance, ('schema','profile','validation','parent_job_id','parent_terminal_required',
        'source_mode','request_sha256','launch_identity_sha256','implementation_sha256',
        'public_context','host_context','export_request','numeric_recipe','expected','numeric_context',
        'backend_request','result_root','result_root_identity','receipt_sha256','cold_report','stages',
        'source_unchanged','geometry_independently_recomputed','quality_accepted',
        'input_guard_scope','dependency_scope'))
    for key, value in dict(schema='toon-selected-image-parent-validation.v1',profile=PROFILE,validation='passed',
            parent_job_id=job.name,parent_terminal_required=True,source_mode='SAVED_DISK_V1',
            implementation_sha256=implementation,result_root=str(bundle),quality_accepted=False,
            source_unchanged=True,geometry_independently_recomputed=False).items():
        _same(acceptance.get(key), value, 'Original acceptance '+key+' differs')
    _same(data['cold_report'], acceptance['cold_report'], 'Original cold report descriptors differ')
    request = held.document(job/'request.json')
    _fields(request, ('schema_version','command','params'))
    _need(request['schema_version'] == '1.0' and request['command'] == 'toon-image.run', 'Original request command differs')
    try:
        original = plain(normalize_params(request['params']))
    except ImageContractError as error:
        raise Failure(error.code, error.path+': '+str(error)) from error
    _same(original, request['params'], 'Historical selected request differs after normalization')
    _need(original['implementation_sha256'] == implementation, 'Original selected request implementation differs')
    export_request, numeric_recipe = split_params(original)
    _same(held.descriptor(acceptance['export_request'], root/'ORIGINAL-EXPORT-REQUEST.json', True),
          export_request, 'Original export declaration differs from request')
    _same(held.descriptor(acceptance['numeric_recipe'], root/'ORIGINAL-NUMERIC-RECIPE.json', True),
          numeric_recipe, 'Original numeric declaration differs from request')
    context = held.descriptor(acceptance['host_context'], root/'HOST-CONTEXT.json', True)
    _fields(context, ('schema','run_root','nonce','export_request_sha256','numeric_recipe_sha256'))
    _same(context['schema'], 'toon-selected-image-host-context.v1', 'Historical host context schema differs')
    _same(context['run_root'], str(root), 'Historical host root differs')
    _need(type(context['nonce']) is str and re.fullmatch('[0-9a-f]{32}', context['nonce']) is not None, 'Historical host nonce differs')
    for key, value in (('export_request_sha256',export_request),('numeric_recipe_sha256',numeric_recipe)):
        _same(context[key], hashlib.sha256(canonical(value)).hexdigest(), 'Historical original declaration digest differs')
    launch = held.document(job/'launch.json')
    _required(launch, ('image_identity','image_request_sha256','blender'))
    identity = launch.get('image_identity')
    _need(type(identity) is dict, 'Historical launch identity required')
    backend, typed_pin = _launch_sources(identity, implementation)
    request_sha = hashlib.sha256(canonical(request)).hexdigest()
    launch_sha = hashlib.sha256(canonical(identity)).hexdigest()
    _same(launch.get('image_request_sha256'), request_sha, 'Original queued request digest differs')
    _same(acceptance.get('request_sha256'), request_sha, 'Original acceptance request differs')
    _same(acceptance.get('launch_identity_sha256'), launch_sha, 'Original acceptance launch differs')
    binaries = identity.get('binaries')
    _need(type(binaries) is list and len(binaries) == 2, 'Historical binary declaration count differs')
    for binary in binaries:
        _fields(binary, ('file','expected_sha256')); _local(binary['file']); _digest(binary['expected_sha256'])
    _same(binaries[1]['file'], launch.get('blender'), 'Historical selected Blender declaration differs')
    public = held.descriptor(acceptance['public_context'], root/'PUBLIC-CONTEXT.json', True)
    _same(public, dict(schema='toon-selected-image-public-context.v1',job_id=job.name,profile=PROFILE,
        run_root=str(root),request_sha256=request_sha,launch_identity_sha256=launch_sha,
        implementation_sha256=implementation,source=dict(file=original['file'],sha256=original['expected_sha256']),
        resources=original['resources'],host_context=acceptance['host_context'],public_job=True,parent_terminal_required=True),
        'Complete historical public context differs')
    # The presealed Expected is consumed before even opening the result lease.
    raw = held.descriptor(acceptance['expected'], root/'EXPECTED.json')
    expected = expected_from_document(raw)
    _same(expected_document(expected, check), raw, 'Historical Expected roundtrip differs')
    numeric = held.descriptor(acceptance['numeric_context'], root/'NUMERIC-CONTEXT.json')
    backend_request = held.descriptor(acceptance['backend_request'], root/'backend-request.json')
    _fields(numeric, ('schema','parent','export_request_sha256','numeric_recipe_sha256','capture_sha256',
        'dependency_audit_sha256','dependency_implementation_sha256','scene_export_sha256','input_sha256',
        'backend_request_sha256','runtime','backend_sources'))
    runtime = numeric['runtime']
    _fields(runtime, ('python','numpy','blender','blender_build'))
    _need(all(type(value) is str and 0 < len(value) <= 4096 for value in runtime.values()), 'Historical target runtime strings required')
    _same(numeric['schema'], 'toon-selected-image-numeric-context.v1', 'Historical numeric context schema differs')
    _same(numeric['parent'], context, 'Historical numeric/host parent differs')
    for key in ('export_request_sha256','numeric_recipe_sha256'):
        _same(numeric[key], context[key], 'Historical numeric original declaration digest differs')
    _same(numeric['dependency_implementation_sha256'], typed_pin, 'Historical typed implementation differs')
    _same(raw['host_context_sha256'], acceptance['numeric_context']['sha256'], 'Historical Expected context digest differs')
    request_bytes = canonical(backend_request)
    _need(expected.canonical_request_utf8 == request_bytes and expected.request_bytes == len(request_bytes), 'Historical Expected request bytes differ')
    _same(expected.request_sha256, hashlib.sha256(request_bytes).hexdigest(), 'Historical canonical request SHA differs')
    _same(expected.request_path, str(root/'backend-request.json'), 'Historical Expected request fixed role differs')
    _same(expected.request_sha256, acceptance['backend_request']['sha256'], 'Historical Expected request digest differs')
    _same(numeric['backend_request_sha256'], expected.request_sha256, 'Historical numeric request digest differs')
    _same(raw['runtime'], {key:runtime[key] for key in ('python','numpy')}, 'Historical Expected runtime differs')
    _same(raw['code_pins'], numeric['backend_sources'], 'Historical Expected/numeric backend pins differ')
    _code_pins(raw['code_pins'], backend)
    policy = dict(model='selected_mesh_sources_all_nondegenerate_two_sided.v1',
        member_object_ids=export_request['context']['member_object_names'],
        visible_enabled=numeric_recipe['visible_enabled'],hidden_enabled=numeric_recipe['hidden_enabled'])
    input_path = root/'export/geometry/toon-image-input.json'
    _same(backend_request, dict(schema='toon-selected-mesh-chain-image-request.v1',
        input=dict(input_path=str(input_path),input_sha256=numeric['input_sha256']),
        selector=numeric_recipe['selector'],policy=policy,chaining=numeric_recipe['chaining'],
        style=numeric_recipe['style'],budgets=numeric_recipe['budgets']), 'Historical backend request differs from original recipe')
    declarations.request_expected(backend_request, raw, check)
    capture = held.document(root/'export/capture.json', _digest(numeric['capture_sha256']))
    _required(capture, ('source','resources','verified_inputs_after','scene_export','input',
        'scene_export_record','runtime','camera','npz'))
    for key in ('scene_export','input'):
        _fields(capture[key], ('file','expected_sha256','bytes'))
    for key, value in dict(schema='toon-selected-image-saved-capture.v1',source_mode='SAVED_DISK_V1',
            declared_request=export_request,inputs_unchanged=True,source_saved=False,numerical_producer_run=False,
            parent_accepted=False,native_source_authenticated=False,runtime=runtime).items():
        _same(capture.get(key), value, 'Historical capture '+key+' differs')
    for key, value in export_request['context'].items():
        _same(capture.get('object_names' if key == 'occluder_object_names' else key), value,
              'Historical capture context differs')
    _same(capture.get('object_ids'), export_request['context']['occluder_object_names'], 'Historical object order differs')
    _same(capture.get('member_object_ids'), export_request['context']['member_object_names'], 'Historical member domain differs')
    _same(capture.get('camera_type'), 'ORTHO', 'Historical capture camera kind differs')
    wanted, total = _historical_sources(capture, export_request)
    audit = held.document(root/'export/resources.json', _digest(numeric['dependency_audit_sha256']))
    _required(audit, ())
    _audit(audit, wanted, total, export_request['context'], runtime, typed_pin)
    _same(numeric['scene_export_sha256'], capture['scene_export']['expected_sha256'], 'Historical scene export digest differs')
    _same(numeric['input_sha256'], capture['input']['expected_sha256'], 'Historical input digest differs')
    geometry, geometry_members = _geometry(held, root, capture, export_request, raw, check)
    stages = acceptance.get('stages')
    _need(type(stages) is list and len(stages) == 3 and all(type(stage) is dict for stage in stages) and
          [stage.get('mode') for stage in stages] == ['identity','export','numeric'], 'Historical owned stage set differs')
    for stage in stages:
        _fields(stage, ('mode','status','lifecycle'))
        _same(stage.get('status'), 'completed', 'Historical stage status differs')
        lifecycle = held.descriptor(stage['lifecycle'], root/('stage-'+stage['mode'])/'lifecycle.json')
        _required(lifecycle, ('status','root_returncode','tree_handle_closed','owned_job_zero_active_observed'))
        _need(lifecycle.get('status') == 'completed' and type(lifecycle.get('root_returncode')) is int and
              lifecycle['root_returncode'] == 0 and lifecycle.get('tree_handle_closed') is True and
              lifecycle.get('owned_job_zero_active_observed') is True, 'Historical owned lifecycle differs')
    original_cold = held.descriptor(acceptance['cold_report'], root/'COLD-REPORT.json', True)
    identity_now = held.directory(bundle).identity()
    _same(identity_now, acceptance.get('result_root_identity'), 'Historical selected bundle identity differs')
    lease = stack.enter_context(SelectedWindowsBundleLease(str(bundle),
        expected_root_identity=(identity_now['volume'],identity_now['file_index']),
        forbidden_file_identities=frozenset(held.identities),reader_api=reader,
        limits=SelectedLeaseLimits(),checkpoint=check))
    cold = reader.verify_selected_chain_image_v1(lease, expected_receipt_sha256=_digest(acceptance['receipt_sha256']),
        expected_run=expected,limits=reader.SelectedReaderLimits(),checkpoint=check)
    _need(cold.parent_accepted is False and cold.quality_accepted is False and
          cold.geometry_recomputed is False and cold.source_current == 'not_checked', 'Selected cold reader exceeded authority')
    fresh = cold_document(cold, check)
    _same(fresh, original_cold, 'Historical selected cold report differs from current verification')

    def recheck():
        try:
            check(); held.recheck()
            _export_members(held, root/'export', check)
            _need(held.flat(geometry) == geometry_members, 'Historical geometry membership changed')
            lease.recheck(); check()
        except BaseException:
            if check.error is not None: raise check.error
            raise

    recheck(); held.directory(own)
    def write(name, value):
        path = own/name; encoded = canonical(value)
        _need(len(encoded) <= _CAP, 'Inspector output metadata cap', 'RESOURCE_LIMIT')
        with path.open('xb') as stream: stream.write(encoded)
        held.file(path, hashlib.sha256(encoded).hexdigest(), maximum=_CAP, size=len(encoded))
        return dict(file=str(path),sha256=hashlib.sha256(encoded).hexdigest(),bytes=len(encoded))
    cold_ref = write('parent-inspect-cold.json', fresh)
    report = dict(schema='toon-image-parent-inspection.v1',profile=PROFILE,
        validation='pinned_original_parent_and_bundle_verified',original_parent_job_id=job.name,
        parent_result=dict(file=str(result_path),expected_sha256=result_sha),implementation_sha256=implementation,
        nonce=context['nonce'],terminal_observation=dict(state='succeeded',exit_code=0,
            status_raw_sha256=held.files[ntpath.normcase(str(job/'status.json'))]['sha256']),
        binding_scope=dict(request='canonical_json_sha256',launch='image_identity_and_image_request_sha256_only',
            status='current_held_terminal_observation'),source_current='not_checked',
        historical_source_unchanged_recorded=acceptance.get('source_unchanged'),parent_accepted=False,
        quality_accepted=False,geometry_recomputed=False,cold_report=cold_ref,original_acceptance=data['acceptance'],
        unverified=['current_source_resources_code_paths','NPZ_header_reparse_and_member_payload_or_geometry_recomputation',
                    'entire_launch_status_historical_bytes','new_inspector_runner_terminal','display_and_aesthetics'])
    report_ref = write('parent-inspection.json', report); recheck()
    return dict(report,inspection_report=report_ref), recheck
