# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete CPU image bundle integrity; no provider or geometry imports.

The host seals a canonical expectation before numerical work. This reader binds
that expectation and fully validates the six output files through HeldFiles'
existing guards. It does not recompute geometry or grant quality/parent status.
"""
from contextlib import ExitStack
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import uuid

PROFILE = 'cpu-boundary-image.v1'
EXPECTATION_SCHEMA = 'toon-cpu-image-expected.v1'
METADATA_SCHEMA = 'toon-cpu-image-metadata.v1'
RECEIPT_SCHEMA = 'toon-cpu-image-receipt.v1'
COLD_SCHEMA = 'toon-cpu-image-cold-report.v1'
CHANNELS = ('v', 'h', 'both', 'off')
FILES = {name: name+'.npy' for name in CHANNELS}
FILES['metadata'] = 'METADATA.json'
INVENTORY = frozenset((*FILES.values(), 'receipt.json'))
BOTH_ABSOLUTE_TOLERANCE = 1e-7
JSON_MAX_DEPTH = 64
JSON_MAX_NODES = 1048576


def _need(value, message):
    if not value:
        raise ValueError(message)


def _json_values(value):
    """Bounded JSON type checks; cycles differ from repeated acyclic values.

    Byte/output budgets still come from the host. These structural parser
    bounds protect Python metadata traversal, not geometry or image quality.
    Dictionary keys count as nodes, as do their values and each container.
    """
    todo = [(value, 0, False)]
    active = set()
    scheduled = 1
    while todo:
        item, depth, leaving = todo.pop()
        if leaving:
            active.remove(id(item))
            continue
        _need(depth <= JSON_MAX_DEPTH, 'JSON structural depth exceeded')
        kind = type(item)
        if item is None or kind in (bool, int):
            continue
        if kind is float:
            _need(math.isfinite(item), 'Nonfinite JSON number')
        elif kind is str:
            item.encode('utf-8', errors='strict')
        elif kind in (dict, list, tuple):
            _need(id(item) not in active, 'Cyclic Python value is not JSON')
            if kind is dict:
                _need(all(type(key) is str for key in item), 'JSON object keys must be strings')
                children = 2*len(item)
            else:
                children = len(item)
            # Refuse before allocating an oversized pending-node stack.
            scheduled += children
            _need(scheduled <= JSON_MAX_NODES, 'JSON structural node count exceeded')
            active.add(id(item))
            todo.append((item, depth, True))
            if kind is dict:
                todo.extend((child, depth+1, False) for pair in item.items() for child in pair)
            else:
                todo.extend((child, depth+1, False) for child in item)
        else:
            raise ValueError('Non-JSON value type: '+kind.__name__)


def canonical(value):
    """Sorted compact strict UTF-8 JSON with exactly one trailing LF."""
    _json_values(value)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def strict_json(data):
    _need(type(data) is bytes, 'JSON input must be bytes')
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Nonfinite JSON number')
    try:
        value = json.loads(data.decode('utf-8', errors='strict'),
            object_pairs_hook=pairs, parse_constant=constant)
        _json_values(value)  # Also rejects overflow such as 1e999 and surrogates.
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid UTF-8 JSON or excessive JSON nesting') from error
    return value


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_bytes(path, payload):
    path = Path(path)
    with path.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(file=path.name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))


def write_document(path, value):
    return _write_bytes(path, canonical(value))


def _digest(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None,
          'Lowercase SHA256 required')
    return value


def _reference(value):
    _need(type(value) is dict and {'file','sha256'} <= value.keys(), 'Explicit file reference required')
    _need(type(value['file']) is str and Path(value['file']).is_absolute(), 'Absolute reference path required')
    _digest(value['sha256'])
    if 'bytes' in value:
        _need(type(value['bytes']) is int and value['bytes'] >= 0, 'Reference byte size required')
    _json_values(value)


def _expectation(expectation_ref, expectation):
    required = {'schema','profile','job_id','nonce','implementation_sha256',
        'provider_implementation_sha256','public_context_ref','request_ref','capture_ref',
        'input_ref','npz_refs','shape','dtype','origin','alpha','max_output_bytes'}
    _need(type(expectation) is dict and required <= expectation.keys(), 'Expectation fields missing')
    _need(expectation['schema'] == EXPECTATION_SCHEMA and expectation['profile'] == PROFILE,
          'Expectation schema/profile differs')
    for key in ('job_id','nonce'):
        _need(type(expectation[key]) is str and bool(expectation[key]), 'Expectation identity required')
    for key in ('implementation_sha256','provider_implementation_sha256'):
        _digest(expectation[key])
    for key in ('public_context_ref','request_ref','capture_ref','input_ref'):
        _reference(expectation[key])
    _need(type(expectation['npz_refs']) is list, 'Explicit NPZ references required')
    for row in expectation['npz_refs']:
        _reference(row)
    shape = expectation['shape']
    _need(type(shape) is list and len(shape) == 3 and
        all(type(n) is int and n > 0 for n in shape) and shape[2] == 4, 'Explicit RGBA shape required')
    _need(expectation['dtype'] == '<f4' and expectation['origin'] == 'bottom_left' and
        expectation['alpha'] == 'premultiplied', 'Output interpretation differs')
    _need(type(expectation['max_output_bytes']) is int and expectation['max_output_bytes'] > 0,
          'Positive host output budget required')
    _reference(expectation_ref)
    payload = canonical(expectation)
    _need(hashlib.sha256(payload).hexdigest() == expectation_ref['sha256'],
          'Expectation is not the canonical host-sealed content')
    if 'bytes' in expectation_ref:
        _need(expectation_ref['bytes'] == len(payload), 'Expectation reference byte size differs')
    return tuple(shape), expectation['max_output_bytes'], payload


def _member(row, name):
    _need(type(row) is dict and set(row) == {'file','sha256','bytes'}, 'Member descriptor fields differ')
    _need(row['file'] == name and type(row['bytes']) is int and row['bytes'] > 0,
          'Member fixed name/byte size differs')
    _digest(row['sha256'])


def _guard_bytes(row, *, maximum, checkpoint):
    _need(type(row['bytes']) is int and 0 <= row['bytes'] <= maximum, 'Held byte extent exceeds bound')
    payload = bytearray()
    digest = hashlib.sha256()
    for block in row['guard'].chunks(checkpoint):
        checkpoint()
        _need(len(payload)+len(block) <= row['bytes'], 'Held payload exceeds declared extent')
        payload.extend(block)
        digest.update(block)
    _need(len(payload) == row['bytes'] and digest.hexdigest() == row['sha256'], 'Held payload changed')
    return bytes(payload)


def _npy(payload, shape):
    import numpy as np
    stream = io.BytesIO(payload)
    _need(np.lib.format.read_magic(stream) == (1,0), 'Only explicit NPY v1 supported')
    actual, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=1024)
    _need(type(actual) is tuple and all(type(n) is int for n in actual) and actual == shape,
          'NPY shape differs')
    _need(fortran is False and dtype.str == '<f4' and not dtype.hasobject, 'NPY C-order float32 required')
    offset = stream.tell()
    _need(len(payload) == offset+math.prod(shape)*4, 'NPY payload extent/trailing bytes differ')
    array = np.frombuffer(payload, dtype='<f4', offset=offset).reshape(shape)
    _pixels(array)
    return array


def _pixels(array):
    import numpy as np
    _need(bool(np.isfinite(array).all()) and bool(((array >= 0) & (array <= 1)).all()) and
          bool((array[:,:,:3] <= array[:,:,3:4]).all()), 'Finite [0,1] premultiplied RGBA required')


def _relations(images, checkpoint):
    import numpy as np
    _need(not bool(np.any(images['off'])), 'Off image must be numeric zero')
    width = images['v'].shape[1]
    rows = max(1, 1048576 // (width*4*4))
    max_error = 0.0
    for begin in range(0, images['v'].shape[0], rows):
        checkpoint()
        end = begin+rows
        visible, hidden, combined = (images[key][begin:end] for key in ('v','h','both'))
        expected = visible+hidden*(np.float32(1)-visible[:,:,3:4])
        error = float(np.abs(expected-combined).max(initial=np.float32(0)))
        max_error = max(max_error,error)
        _need(error <= BOTH_ABSOLUTE_TOLERANCE, 'Both image differs from float32 visible-over-hidden')
    return max_error


def _metadata(value):
    _need(type(value) is dict and set(value) == {'schema','profile','frame','render_metrics'},
          'Metadata fields differ')
    _need(value['schema'] == METADATA_SCHEMA and value['profile'] == PROFILE, 'Metadata schema/profile differs')
    _need(type(value['frame']) is dict and type(value['render_metrics']) is dict,
          'Opaque frame and render metrics must be JSON objects')
    _json_values(value)


def _receipt(expectation_ref, members):
    value = dict(schema=RECEIPT_SCHEMA, profile=PROFILE, expectation=expectation_ref,
                 members=members, output_bytes=0)
    fixed = sum(row['bytes'] for row in members.values())
    while True:
        payload = canonical(value)
        total = fixed+len(payload)
        if total == value['output_bytes']:
            return value, payload
        value['output_bytes'] = total


def _bundle_path(bundle):
    path = Path(bundle)
    _need(path.is_absolute() and not path.is_symlink(), 'Absolute ordinary bundle directory required')
    return path


def produce_bundle(bundle, images, frame_record, render_metrics, *, expectation_ref,
                   expectation, checkpoint=lambda: None):
    """Create six files, full readback, receipt last; partial output has no receipt.

    The caller owns the new/empty bundle path. The host expectation must have
    been serialized using canonical/write_document before this function.
    No source/geometry truth is inferred from frame_record or output pixels.
    """
    import numpy as np
    from filesystem import FileGuard
    _need(callable(checkpoint), 'checkpoint must be callable')
    checkpoint()
    shape, maximum, _ = _expectation(expectation_ref, expectation)
    _need(type(images) is dict and set(images) == set(CHANNELS), 'Four explicit image arrays required')
    metadata = dict(schema=METADATA_SCHEMA,profile=PROFILE,frame=frame_record,render_metrics=render_metrics)
    _metadata(metadata)
    metadata_payload = canonical(metadata)
    declared = {}
    for key in CHANNELS:
        array = images[key]
        _need(isinstance(array,np.ndarray) and array.shape == shape and array.dtype.str == '<f4' and
              array.flags.c_contiguous and not array.dtype.hasobject, 'Producer C-order float32 RGBA required')
        _pixels(array)
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header,np.lib.format.header_data_from_array_1_0(array))
        declared[key] = dict(file=FILES[key],sha256='0'*64,bytes=len(header.getvalue())+array.nbytes)
    declared['metadata'] = dict(file=FILES['metadata'],sha256=hashlib.sha256(metadata_payload).hexdigest(),
                                bytes=len(metadata_payload))
    predicted, _ = _receipt(expectation_ref,declared)
    _need(predicted['output_bytes'] <= maximum, 'Complete bundle exceeds host output budget')
    _relations(images,checkpoint)
    path = _bundle_path(bundle)
    if path.exists():
        _need(path.is_dir() and not any(path.iterdir()), 'New or empty bundle required')
    else:
        _need(path.parent.is_dir(), 'Bundle parent must exist')
        path.mkdir()
    members = {}
    with ExitStack() as stack:
        stack.enter_context(FileGuard(path,directory=True))
        arrays = {}
        for key in CHANNELS:
            checkpoint()
            target = path/FILES[key]
            with target.open('xb') as stream:
                np.save(stream,images[key],allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            guard = stack.enter_context(FileGuard(target))
            info = guard._information()
            size = (info.size_high << 32) | info.size_low
            _need(size == declared[key]['bytes'], 'Saved NPY extent differs from declared output')
            digest = guard.sha256(checkpoint)
            row = dict(guard=guard,bytes=size,sha256=digest)
            payload = _guard_bytes(row,maximum=maximum,checkpoint=checkpoint)
            arrays[key] = _npy(payload,shape)
            members[key] = dict(file=FILES[key],bytes=size,sha256=digest)
        _relations(arrays,checkpoint)
        members['metadata'] = _write_bytes(path/FILES['metadata'],metadata_payload)
        guard = stack.enter_context(FileGuard(path/FILES['metadata']))
        row = dict(members['metadata'],guard=guard)
        _metadata(strict_json(_guard_bytes(row,maximum=maximum,checkpoint=checkpoint)))
        receipt, payload = _receipt(expectation_ref,members)
        _need(receipt['output_bytes'] == predicted['output_bytes'] and receipt['output_bytes'] <= maximum,
              'Actual complete output exceeds host budget or predicted extent')
        temporary = path/('.receipt.pending-'+uuid.uuid4().hex)
        _write_bytes(temporary,payload)
        checkpoint()
        _need(set(p.name for p in path.iterdir()) == set(FILES.values()) | {temporary.name},
              'Unexpected producer bundle member')
        _need(not os.path.lexists(path/'receipt.json'), 'Receipt collision')
        os.rename(temporary,path/'receipt.json')
    return dict(file='receipt.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())


def verify_bundle(bundle, *, receipt_sha256, expectation_ref, expectation, held,
                  checkpoint=lambda: None):
    """Full cold integrity verification using retained handles only.

    Caller retains HeldFiles/ExitStack through final parent acceptance. This
    function never imports the CPU provider, bpy, numerical kernels or a DLL.
    Source-current checks belong to the host, not this image-integrity reader.
    """
    _need(callable(checkpoint), 'checkpoint must be callable')
    checkpoint()
    shape, maximum, sealed = _expectation(expectation_ref,expectation)
    path = _bundle_path(bundle)
    held.directory(path)
    _need(held.flat_names(path) == set(INVENTORY), 'Bundle inventory differs')
    expected_row = held.file(expectation_ref['file'],expectation_ref['sha256'],maximum=len(sealed),size=len(sealed))
    observed = strict_json(_guard_bytes(expected_row,maximum=len(sealed),checkpoint=checkpoint))
    _need(canonical(observed) == sealed, 'Held expectation differs from host object')
    receipt_row = held.file(path/'receipt.json',_digest(receipt_sha256),maximum=maximum)
    receipt = strict_json(_guard_bytes(receipt_row,maximum=maximum,checkpoint=checkpoint))
    _need(type(receipt) is dict and set(receipt) == {'schema','profile','expectation','members','output_bytes'},
          'Receipt fields differ')
    _need(receipt['schema'] == RECEIPT_SCHEMA and receipt['profile'] == PROFILE, 'Receipt schema/profile differs')
    _need(canonical(receipt['expectation']) == canonical(expectation_ref), 'Receipt expectation reference differs')
    members = receipt['members']
    _need(type(members) is dict and set(members) == set(FILES), 'Receipt member set differs')
    for key,name in FILES.items():
        _member(members[key],name)
    total = receipt_row['bytes']+sum(row['bytes'] for row in members.values())
    _need(type(receipt['output_bytes']) is int and receipt['output_bytes'] == total and total <= maximum,
          'Complete bundle byte budget/accounting differs')
    rows = {key:held.file(path/FILES[key],row['sha256'],maximum=maximum,size=row['bytes'])
            for key,row in members.items()}
    _need(held.flat_names(path) == set(INVENTORY), 'Bundle inventory changed while acquiring files')
    metadata = strict_json(_guard_bytes(rows['metadata'],maximum=maximum,checkpoint=checkpoint))
    _metadata(metadata)
    arrays = {key:_npy(_guard_bytes(rows[key],maximum=maximum,checkpoint=checkpoint),shape) for key in CHANNELS}
    max_error = _relations(arrays,checkpoint)
    _need(held.flat_names(path) == set(INVENTORY), 'Bundle inventory changed while verifying files')
    checkpoint()
    hashes = {FILES[key]:row['sha256'] for key,row in members.items()}
    hashes['receipt.json'] = receipt_sha256
    return dict(schema=COLD_SCHEMA,validation='complete_bundle_integrity',receipt_sha256=receipt_sha256,
        expectation_sha256=expectation_ref['sha256'],shape=list(shape),output_bytes=total,
        member_sha256=hashes,parent_accepted=False,quality_accepted=False,geometry_recomputed=False,
        source_current='not_checked',qualification=dict(
            scope='complete six-file integrity; opaque metadata is not a mathematical or geometric witness',
            pixel_checks='complete NPY header/payload, finite [0,1] premultiplied values, numeric-zero off',
            both_relation='float32 v + h * (1 - v.alpha)',both_absolute_tolerance=BOTH_ABSOLUTE_TOLERANCE,
            both_observed_max_absolute_error=max_error,
            retention='six files and bundle directory held by caller; before/after fixed inventory checks are not an OS directory transaction'))
