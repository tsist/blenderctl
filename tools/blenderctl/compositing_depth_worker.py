"""Isolated NumPy/OIIO depth worker; no bpy/Torch/model/network imports."""
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time
import types
import zlib


class WorkerError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def need(value, message, code='WORKER_REJECTED'):
    if not value:
        raise WorkerError(code, message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def strict(data):
    def pairs(rows):
        out = {}
        for key, value in rows:
            need(key not in out, 'Duplicate binding JSON field')
            out[key] = value
        return out
    def bad(token):
        raise WorkerError('INVALID_JSON', 'Nonfinite token ' + token)
    value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=bad)
    def finite(item):
        if type(item) in (int, float):
            try:
                need(math.isfinite(item), 'Nonfinite JSON number')
            except OverflowError as exc:
                raise WorkerError('INVALID_JSON', 'Unrepresentable JSON number') from exc
        elif type(item) is dict:
            for child in item.values():
                finite(child)
        elif type(item) is list:
            for child in item:
                finite(child)
    finite(value)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def pinned_module(name, row):
    path = Path(row['file'])
    data = path.read_bytes()
    need(hashlib.sha256(data).hexdigest() == row['sha256'] and len(data) == row['bytes'], 'Code source bytes changed')
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def run(job):
    started = time.monotonic()
    job = Path(job)
    need(job.is_absolute() and job.is_dir(), 'Absolute existing owned job required')
    job = job.resolve()
    out = job / 'depth'
    need(out.is_dir(), 'Host must create job/depth')
    need(sys.flags.isolated and sys.flags.dont_write_bytecode and sys.flags.no_user_site,
         'Worker requires Python -I -B')
    need(sys.pycache_prefix is not None, 'Exclusive pycache prefix required')
    prefix = Path(sys.pycache_prefix).resolve()
    need(prefix.is_relative_to(out) and prefix != out and (not prefix.exists() or prefix.is_dir() and not any(prefix.iterdir())),
         'Worker pycache must be new/empty in owned depth output')
    binding_path = out / 'binding.json'
    binding_bytes = binding_path.read_bytes()
    binding_digest = hashlib.sha256(binding_bytes).hexdigest()
    binding = strict(binding_bytes)
    need(type(binding) is dict and binding.get('schema') == 'depth-worker-binding-v1', 'Binding schema differs')
    operation = binding.get('operation')
    need(operation in ('depth-prepare', 'depth-inspect'), 'Binding operation differs')
    need(type(binding.get('request')) is dict and set(binding['request']) == {'params'}, 'request.params required')
    params = binding['request']['params']
    signature = hashlib.sha256(canonical(params)).hexdigest()
    need(type(binding.get('request_signature')) is str and binding['request_signature'] == signature, 'Binding signature mismatch')
    timeout = binding.get('timeout_seconds')
    need(type(timeout) is int and 1 <= timeout <= 1200, 'Effective integer worker timeout required')
    protected = {binding_path: binding_digest}

    def checkpoint():
        need(not (job / 'cancel.request').exists() and not (job / 'depth.cancel.request').exists()
             and not (out / 'depth.cancel.request').exists(), 'Cancellation requested', 'CANCELLED')
        need(time.monotonic() - started <= timeout, 'Worker wall budget expired', 'TIMEOUT')

    def protect(row, sized=False):
        checkpoint()
        need(type(row) is dict and type(row.get('file')) is str and type(row.get('sha256')) is str, 'Input/code row required')
        path = Path(row['file'])
        need(path.is_absolute() and path.is_file() and len(row['sha256']) == 64
             and all(c in '0123456789abcdef' for c in row['sha256']), 'Absolute pinned input required')
        path = path.resolve()
        if path in protected:
            need(protected[path] == row['sha256'], 'Conflicting protected input identity')
        need(sha(path) == row['sha256'], 'Protected input SHA mismatch', 'INPUT_CHANGED')
        if sized:
            need(type(row.get('bytes')) is int and row['bytes'] >= 0 and path.stat().st_size == row['bytes'], 'Protected byte size differs')
        protected[path] = row['sha256']
        return path

    checkpoint()
    need(type(binding.get('input_files')) is list, 'Host expanded input_files required')
    for row in binding['input_files']:
        protect(row)
    need(type(binding.get('codes')) is list, 'Code closure required')
    codes = {}
    for row in binding['codes']:
        need(type(row) is dict and type(row.get('role')) is str and row['role'] not in codes, 'Duplicate/invalid code role')
        protect(row, True)
        codes[row['role']] = row
    need(set(('depth_contract', 'depth_math', 'depth_io', 'depth_worker')) <= set(codes), 'Required code closure missing')
    need(Path(codes['depth_worker']['file']).resolve() == Path(__file__).resolve(), 'Worker code identity differs')
    runtime = binding.get('runtime')
    need(type(runtime) is dict and type(runtime.get('python')) is dict, 'Host runtime Python identity required')
    need(Path(runtime['python']['file']).resolve() == Path(sys.executable).resolve(), 'Actual Python executable differs')
    protect(runtime['python'], True)
    contract = pinned_module('owned_depth_contract', codes['depth_contract'])
    io = pinned_module('owned_depth_io', codes['depth_io'])
    np = io.np
    metadata = []

    def read(desc, domain, kind='scalar'):
        path = Path(desc['file']).resolve()
        need(protected.get(path) == desc['expected_sha256'], 'Direct descriptor missing Host expanded input protection')
        checkpoint()
        array, meta = io.read_scalar(desc, domain, kind)
        metadata.append(meta)
        checkpoint()
        return array

    def read_imported_normal(desc, domain):
        path = Path(desc['file']).resolve()
        need(protected.get(path) == desc['expected_sha256'],
             'Direct Normal descriptor missing Host expanded input protection')
        checkpoint()
        array, meta = io.read_imported_normal(desc, domain)
        metadata.append(meta)
        checkpoint()
        return array

    def member(path, role, array):
        return {'role': role, 'file': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size,
                'dtype': str(array.dtype), 'shape': list(array.shape)}

    def save_array(name, role, array):
        checkpoint()
        path = out / name
        with path.open('xb') as stream:
            np.save(stream, array, allow_pickle=False)
        restored = io.read_npy(path, array.shape, (array.dtype,))
        need(np.array_equal(array, restored), 'Array write/readback differs')
        checkpoint()
        return member(path, role, restored), restored

    def save_png(name, role, pixels, domain):
        checkpoint()
        need(pixels.dtype == np.uint8 and pixels.ndim == 3 and pixels.shape[2] in (1, 3), 'Diagnostic PNG8 array required')
        path = out / name
        h, w, channels = pixels.shape
        def chunk(tag, data):
            return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        scanlines = b''.join(b'\x00' + row.tobytes() for row in pixels)
        encoded = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0 if channels == 1 else 2, 0, 0, 0))
        encoded += chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b'')
        with path.open('xb') as stream:
            stream.write(encoded)
        restored, meta = io.read_png(path, domain, channels)
        need(np.array_equal(restored, pixels), 'Preview complete pixel readback differs')
        metadata.append(meta)
        checkpoint()
        return member(path, role, restored)

    members = []
    if operation == 'depth-prepare':
        params = contract.normalize_prepare(params)
        need(timeout <= params['budget']['timeout_seconds'], 'Worker budget exceeds request')
        domain = params['domain']
        need(domain['width'] * domain['height'] <= params['budget']['max_pixels'], 'Pixel budget exceeded')
        rgb_path = Path(params['rgb']['file']).resolve()
        need(protected.get(rgb_path) == params['rgb']['expected_sha256'], 'RGB missing input protection')
        rgb, rgb_meta = io.read_rgb(params['rgb'], domain)
        metadata.append(rgb_meta)
        checkpoint()
        raw = read(params['depth'], domain)
        if params['validity']['kind'] == 'explicit_mask':
            valid = read(params['validity']['mask'], domain, 'validity')
        else:
            valid = np.ones(raw.shape, dtype=bool)
        masks = [read(edit['mask'], domain, 'edit') for edit in params['edits']]
        boundary_desc = params['normal'].get('boundary_exclusion')
        boundary = read(boundary_desc, domain, 'boundary') if boundary_desc is not None else None
        math_module = pinned_module('owned_depth_math', codes['depth_math'])
        checkpoint()
        if params['normal']['method'] == 'import_geometry':
            imported_normal = read_imported_normal(params['normal']['data'], domain)
            imported_validity = read(params['normal']['normal_validity'], domain, 'validity')
            arrays = math_module.prepare_arrays(raw, valid, params, masks, boundary,
                       imported_normal=imported_normal, imported_normal_validity=imported_validity)
        else:
            arrays = math_module.prepare_arrays(raw, valid, params, masks, boundary)
        checkpoint()
        io.validate_derived(arrays, params)
        restored = {}
        for role, name in (('scalar', 'scalar.npy'), ('validity', 'validity.npy')):
            row, restored[role] = save_array(name, role, arrays[role])
            members.append(row)
        if arrays['normal'] is not None:
            for role, name in (('normal', 'normal.npy'), ('normal_validity', 'normal-validity.npy')):
                row, restored[role] = save_array(name, role, arrays[role])
                members.append(row)
        else:
            restored.update(normal=None, normal_validity=np.zeros(raw.shape, dtype=bool))
        readback = io.validate_derived(restored, params)
        scalar_preview = np.zeros(raw.shape, dtype=np.uint8)
        samples = restored['scalar'][restored['validity']].astype(np.float64)
        lo, hi = float(samples.min()), float(samples.max())
        if hi > lo:
            scalar_preview[restored['validity']] = np.rint((samples - lo) / (hi - lo) * 255).astype(np.uint8)
        members.append(save_png('scalar-preview.png', 'scalar_preview', scalar_preview[:, :, None], domain))
        members.append(save_png('validity-preview.png', 'validity_preview', (restored['validity'].astype(np.uint8) * 255)[:, :, None], domain))
        if restored['normal'] is not None:
            pixels = np.zeros(raw.shape + (3,), dtype=np.uint8)
            pixels[restored['normal_validity']] = np.rint(np.clip(restored['normal'][restored['normal_validity']].astype(np.float64) * .5 + .5, 0, 1) * 255).astype(np.uint8)
            members.append(save_png('normal-preview.png', 'normal_preview', pixels, domain))
        stats = dict(readback, math_statistics=arrays['statistics'], readback_statistics=readback,
                     scalar_preview_range=[lo, hi], previews_are_display_only=True)
        stage_parameters = arrays['stage_parameters']
        cold = False
    else:
        contract.normalize_inspect(params)
        bundle = binding.get('inspect_bundle')
        need(type(bundle) is dict and type(bundle.get('request')) is dict and 'params' in bundle['request'], 'Inspection bundle request missing')
        original = contract.normalize_prepare(bundle['request']['params'])
        domain = original['domain']
        need(domain['width'] * domain['height'] <= original['budget']['max_pixels'], 'Bundle pixel budget differs')
        # Cold inspection decodes original containers as well as derived files.
        # It consumes frozen numerical inputs without normalization or derivation.
        rgb_path = Path(original['rgb']['file']).resolve()
        need(protected.get(rgb_path) == original['rgb']['expected_sha256'], 'Original RGB missing input protection')
        checkpoint()
        _, rgb_meta = io.read_rgb(original['rgb'], domain)
        metadata.append(rgb_meta)
        checkpoint()
        original_raw = read(original['depth'], domain)
        if original['validity']['kind'] == 'explicit_mask':
            original_validity = read(original['validity']['mask'], domain, 'validity')
        else:
            original_validity = np.ones(original_raw.shape, dtype=bool)
        if original['normal']['method'] == 'import_geometry':
            source_normal = read_imported_normal(original['normal']['data'], domain)
            source_nv = read(original['normal']['normal_validity'], domain, 'validity')
            io.validate_imported_normal(source_normal, source_nv, original_validity, original)
        for edit in original['edits']:
            read(edit['mask'], domain, 'edit')
        original_boundary = original['normal'].get('boundary_exclusion')
        if original_boundary is not None:
            read(original_boundary, domain, 'boundary')
        need(type(bundle.get('members')) is list, 'Inspection members missing')
        indexed = {}
        for row in bundle['members']:
            need(type(row) is dict and type(row.get('role')) is str and row['role'] not in indexed, 'Duplicate member role')
            need(row['role'] in ('scalar', 'validity', 'normal', 'normal_validity', 'scalar_preview', 'validity_preview', 'normal_preview'), 'Unknown derived member role')
            path = Path(row['file']).resolve()
            need(protected.get(path) == row['sha256'], 'Bundle member missing input protection')
            protect(row, True)
            indexed[row['role']] = row
        expected_roles = {'scalar', 'validity', 'scalar_preview', 'validity_preview'}
        if original['normal']['method'] != 'none':
            expected_roles |= {'normal', 'normal_validity', 'normal_preview'}
        need(set(indexed) == expected_roles, 'Frozen derived member set differs')
        restored = {}
        for role, dtype in (('scalar', 'float32'), ('validity', 'bool'), ('normal', 'float32'), ('normal_validity', 'bool')):
            if role not in indexed:
                continue
            row = indexed[role]
            shape = (domain['height'], domain['width']) + ((3,) if role == 'normal' else ())
            need(row['dtype'] == dtype and row['shape'] == list(shape), 'Declared member dtype/shape differs')
            restored[role] = io.read_npy(row['file'], shape, (dtype,))
            checkpoint()
        if 'normal' not in restored:
            restored.update(normal=None, normal_validity=np.zeros((domain['height'], domain['width']), dtype=bool))
        readback = io.validate_derived(restored, original)
        for role in ('scalar_preview', 'validity_preview', 'normal_preview'):
            if role in indexed:
                row = indexed[role]
                channels = 3 if role == 'normal_preview' else 1
                pixels, meta = io.read_png(row['file'], domain, channels)
                need(row['dtype'] == 'uint8' and row['shape'] == list(pixels.shape), 'Preview declared dtype/shape differs')
                metadata.append(meta)
                checkpoint()
        members = bundle['members']
        stats = dict(readback, readback_statistics=readback, previews_are_display_only=True)
        stage_parameters = bundle.get('stage_parameters', {})
        cold = True
    checkpoint()
    for path, digest in protected.items():
        checkpoint()
        need(sha(path) == digest, 'Protected input changed during worker', 'INPUT_CHANGED')
    for row in members:
        need(sha(row['file']) == row['sha256'] and Path(row['file']).stat().st_size == row['bytes'], 'Output/member changed during readback')
    checkpoint()
    report = {'status': 'passed', 'operation': operation, 'request_signature': signature, 'members': members,
              'stats': stats, 'stage_parameters': stage_parameters, 'runtime_versions': {'python': sys.version,
                   'executable': sys.executable, 'numpy': np.__version__, 'OpenImageIO': io.oiio.VERSION_STRING,
                   'isolated': bool(sys.flags.isolated), 'dont_write_bytecode': bool(sys.flags.dont_write_bytecode)},
              'cold_read_only': cold, 'input_decode_metadata': metadata,
              'verified_input_SHA': {str(path): digest for path, digest in protected.items()},
              'consumer_eligible': False, 'geometry_or_metric_qualification_assigned': False,
              'seconds': time.monotonic() - started}
    with (out / 'worker-report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return report


def main():
    need(len(sys.argv) == 2, 'One absolute job argument required')
    job = Path(sys.argv[1])
    try:
        report = run(job)
        print(json.dumps({'status': report['status'], 'operation': report['operation'], 'request_signature': report['request_signature']}, allow_nan=False))
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, ImportError, OverflowError) as exc:
        domain_code = getattr(exc, 'code', type(exc).__name__)
        if domain_code in ('TIMEOUT', 'CANCELLED', 'CONFLICT', 'IO_ERROR'):
            public_code = domain_code
        elif isinstance(exc, FileExistsError):
            public_code = 'CONFLICT'
        elif isinstance(exc, OSError):
            public_code = 'IO_ERROR'
        else:
            public_code = 'VALIDATION_FAILED'
        report = {'status': 'failed', 'code': public_code, 'domain_code': domain_code, 'detail': str(exc),
                  'error': {'code': public_code, 'domain_code': domain_code, 'message': str(exc)}, 'consumer_eligible': False}
        path = job / 'depth/worker-report.json'
        try:
            if job.is_absolute() and path.parent.is_dir():
                with path.open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        except (OSError, ValueError, TypeError) as diagnostic_error:
            report['secondary_diagnostic_error'] = {'type': type(diagnostic_error).__name__, 'message': str(diagnostic_error)}
        try:
            print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        except (OSError, ValueError, TypeError, UnicodeError) as stdout_error:
            report['secondary_stdout_error'] = {'type': type(stdout_error).__name__, 'message': str(stdout_error)}
            try:
                sys.stderr.write(json.dumps(report, ensure_ascii=False, allow_nan=False) + '\n')
            except (OSError, ValueError, TypeError, UnicodeError):
                pass
        return {'TIMEOUT': 6, 'CANCELLED': 7, 'CONFLICT': 4, 'IO_ERROR': 9, 'VALIDATION_FAILED': 8}[public_code]


if __name__ == '__main__':
    raise SystemExit(main())
