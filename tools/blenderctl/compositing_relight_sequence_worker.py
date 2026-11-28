"""Owned finite sequence CPU IO/CG profile. No bpy, Torch, model or network.

One frame's arrays are resident at a time; shot statistics use a streaming first
pass. A later CUDA owner can consume these NPY records without changing the
source/frame/statistics identities or weakening the single-frame model guard.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import types
import zlib


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def need(condition, message, code='VALIDATION_FAILED'):
    if not condition:
        exc = RuntimeError(message)
        exc.code = code
        raise exc


def publish_checkpoint(path, value):
    temp = path.with_suffix('.partial')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(temp, path)


def run(job):
    target = job / 'depth'
    b = json.loads((target / 'binding.json').read_text(encoding='utf-8'))
    started = time.monotonic()
    need(sys.flags.isolated and sys.flags.dont_write_bytecode, 'Isolated no-pyc worker required')
    need(Path(sys.executable).resolve() == Path(b['runtime']['python']['file']).resolve() and
         sha(sys.executable) == b['runtime']['python']['sha256'], 'Bundled worker executable differs')
    need(Path(sys.pycache_prefix).is_dir() and not any(Path(sys.pycache_prefix).iterdir()), 'Empty pycache required')

    def checkpoint():
        need(not (job / 'depth.cancel.request').exists(), 'Sequence cancelled', 'CANCELLED')
        need(time.monotonic() - started <= b['timeout_seconds'], 'Sequence timed out', 'TIMEOUT')

    def offline(event, args):
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'urllib.Request', 'http.client.connect'):
            raise RuntimeError('Network forbidden')
    sys.addaudithook(offline)
    protected = {}
    for r in b['input_files']:
        checkpoint()
        need(sha(r['file']) == r['sha256'], 'Protected input SHA differs', 'CONFLICT')
        protected[os.path.normcase(os.path.abspath(r['file']))] = r['sha256']
    codes = {r['role']: r for r in b['codes']}

    def pin(name, role):
        r = codes[role]
        data = Path(r['file']).read_bytes()
        need(hashlib.sha256(data).hexdigest() == r['sha256'], 'Frozen code differs', 'CONFLICT')
        module = types.ModuleType(name)
        module.__file__ = r['file']
        exec(compile(data, r['file'], 'exec'), module.__dict__)
        return module
    io = pin('sequence_io', 'depth_io')
    math = pin('sequence_math', 'depth_math')
    kernel = pin('sequence_kernel', 'sequence:kernel')
    color = pin('sequence_color', 'sequence:color')
    np = io.np
    need(sha(__file__) == codes['depth_worker']['sha256'], 'Actual sequence worker differs')
    params = b['params']
    domain = b['domain']
    hw = domain['height'], domain['width']
    members, frames = [], []

    def load(d, shape, dtype):
        checkpoint()
        need(protected.get(os.path.normcase(os.path.abspath(d['file']))) == d['expected_sha256'], 'Unprotected data')
        return io.read_npy(d['file'], shape, (dtype,))

    def record(path, role, array, stage):
        r = {'file': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size,
             'role': role, 'shape': list(array.shape), 'dtype': str(array.dtype), 'stage_frame': stage}
        members.append(r)
        return r

    def save(folder, name, role, array, stage):
        checkpoint()
        x = np.ascontiguousarray(array)
        path = folder / name
        with path.open('xb') as stream:
            np.save(stream, x, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        restored = io.read_npy(path, x.shape, (x.dtype,))
        need(np.array_equal(x, restored, equal_nan=True), 'NPY complete readback differs')
        return record(path, role, x, stage)

    def write_png(path, rgba):
        linear = np.clip(rgba[..., :3].astype(np.float64), 0, 1)
        encoded = np.where(linear <= .0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - .055)
        pixels = np.concatenate((np.rint(encoded * 255).astype(np.uint8),
                                 np.rint(rgba[..., 3:4] * 255).astype(np.uint8)), axis=-1)
        def chunk(tag, data):
            return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', hw[1], hw[0], 8, 6, 0, 0, 0))
        data += chunk(b'sRGB', b'\x00')
        data += chunk(b'IDAT', zlib.compress(b''.join(b'\x00' + r.tobytes() for r in pixels))) + chunk(b'IEND', b'')
        with path.open('xb') as stream:
            stream.write(data)
        restored, metadata = color.read(io, path, domain, 'png', 4)
        need(np.array_equal(restored, pixels), 'PNG complete readback differs')
        return restored

    if b['mode'] == 'pack':
        cg = params['source']['kind'] == 'declared_CG_geometry_pass'
        reused_raw = b.get('reused_raw', [])
        for r in reused_raw:
            raw = load({'file': r['file'], 'expected_sha256': r['sha256']}, hw, np.float32)
            need(np.isfinite(raw).all(), 'Completed reused raw must be finite FP32')
            del raw
        for f in params['frames']:
            i = f['stage_frame']
            folder = target / ('f%03d' % i)
            folder.mkdir(exist_ok=False)
            arrays = {}
            if cg:
                rgba = load(f['rgb'], hw + (4,), np.float32)
                raw = load(f['depth'], hw, np.float32)
                valid = load(f['validity'], hw, np.bool_)
                imported = load(f['normal'], hw + (3,), np.float32)
                nv = load(f['normal_validity'], hw, np.bool_)
                oracle = {'depth_prepare_version': '1.1', 'domain': domain,
                    'normal': {'method': 'import_geometry'}, 'validity': {'kind': 'explicit_mask'},
                    'semantics': {'measure': 'axial_z', 'near_order': 'smaller_nearer', 'units': 'scene_unit'}}
                io.validate_imported_normal(imported, nv, valid, oracle)
                rotation = np.asarray(f['camera_to_world'], dtype=np.float64)[:3, :3]
                need(np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6, rtol=0) and
                     abs(float(np.linalg.det(rotation)) - 1) <= 1e-6, 'Rigid camera transform required')
                derived = math.prepare_arrays(raw, valid, {'domain': domain, 'budget': params['budget'],
                    'semantics': oracle['semantics'], 'normalization': {'method': 'none'}, 'edits': [],
                    'normal': {'method': 'central_perspective', **f['K'],
                               'max_relative_jump': params['max_relative_jump'], 'boundary_exclusion': None}}, [])
                arrays.update(raw=raw, scalar=derived['scalar'], validity=valid,
                    normal_imported=imported, normal_imported_validity=nv,
                    normal_derived=derived['normal'], normal_derived_validity=derived['normal_validity'])
            else:
                pixels, _ = color.rgb(io, f['rgb'], domain)
                need(pixels.shape[2] == 3 or (pixels[..., 3] == 255).all(), 'DAV2 refuses nonopaque Alpha')
                rgb = np.ascontiguousarray(pixels[..., :3])
                linear = rgb.astype(np.float64) / 255
                linear = np.where(linear <= .04045, linear / 12.92, ((linear + .055) / 1.055) ** 2.4)
                rgba = np.concatenate((linear.astype(np.float32), np.ones(hw + (1,), np.float32)), axis=-1)
                arrays['model_rgb8'] = rgb
            need(np.isfinite(rgba).all() and ((rgba[..., 3] >= 0) & (rgba[..., 3] <= 1)).all(), 'Finite source RGBA required')
            arrays['source_rgba'] = rgba
            for kind in ('light', 'highlight', 'line'):
                d = f['masks'][kind]
                mask = np.full(hw, 0 if kind == 'line' else 1, np.float32) if d is None else load(d, hw, np.float32)
                need(np.isfinite(mask).all() and ((mask >= 0) & (mask <= 1)).all(), 'Finite float32 masks required')
                arrays[kind + '_mask'] = mask
            for role, x in arrays.items():
                save(folder, role + '.npy', role, x, i)
            frames.append({'stage_frame': i, 'roles': list(arrays)})
            del arrays
        stats = {'IO_stage': 'pack', 'max_resident_source_frames': 1, 'source_is_albedo': False,
                 'completed_raw_numeric_admission_count': len(reused_raw)}
    elif b['mode'] == 'encode':
        for f in params['frames']:
            i = f['stage_frame']
            folder = target / ('f%03d' % i)
            folder.mkdir(exist_ok=False)
            selected = [r for r in b['computed'] if r['stage_frame'] == i]
            outrow = next(r for r in selected if r['role'] == 'output')
            source = next(r for r in b['packed'] if r['stage_frame'] == i and r['role'] == 'source_rgba')
            rgba = load({'file': outrow['file'], 'expected_sha256': outrow['sha256']}, hw + (4,), np.float32)
            original = load({'file': source['file'], 'expected_sha256': source['sha256']}, hw + (4,), np.float32)
            need(np.isfinite(rgba).all() and np.array_equal(rgba[..., 3], original[..., 3]), 'Final Alpha changed')
            exr = folder / 'output.exr'
            color.write_linear_straight_exr(io, exr, np.ascontiguousarray(rgba), domain, checkpoint)
            restored, _ = color.read_linear_straight_exr(io, exr, domain)
            need(np.array_equal(rgba, restored), 'Final EXR readback differs')
            record(exr, 'linear_straight_exr', restored, i)
            png = folder / 'output.png'
            record(png, 'display_srgb_png', write_png(png, rgba), i)
        stats = {'IO_stage': 'encode', 'lossless_EXR_readback': True, 'display_transform': 'exact sRGB transfer, display clamp only'}
    elif b['mode'] == 'inspect':
        # Host verified the complete inventory/authority before this cold decode.
        bundle = b['bundle']
        for r in bundle['members']:
            checkpoint()
            shape = tuple(r['shape'])
            if r['role'] == 'linear_straight_exr':
                x, _ = color.read_linear_straight_exr(io, Path(r['file']), domain)
            elif r['role'] == 'display_srgb_png':
                x, _ = color.read(io, Path(r['file']), domain, 'png', 4)
            else:
                x = io.read_npy(r['file'], shape, (np.dtype(r['dtype']),))
            need(x.shape == shape and str(x.dtype) == r['dtype'], 'Cold member shape/type differs')
        visits, previous = [], {}
        for i in params['frame_order']:
            frame = bundle['frames'][i]
            selected = [r for r in bundle['members'] if r['stage_frame'] == i]
            visit_hashes = []
            for r in selected:
                checkpoint()
                if r['role'] == 'linear_straight_exr':
                    x, _ = color.read_linear_straight_exr(io, Path(r['file']), domain)
                elif r['role'] == 'display_srgb_png':
                    x, _ = color.read(io, Path(r['file']), domain, 'png', 4)
                else:
                    x = io.read_npy(r['file'], tuple(r['shape']), (np.dtype(r['dtype']),))
                need(sha(r['file']) == r['sha256'], 'Cold visit bytes differ', 'CONFLICT')
                visit_hashes.append({'role': r['role'], 'file_sha256': r['sha256'],
                                     'decoded_sha256': hashlib.sha256(x.tobytes(order='C')).hexdigest()})
            if i in previous:
                need(visit_hashes == previous[i], 'Repeated cold frame decoded differently', 'CONFLICT')
            previous[i] = visit_hashes
            visits.append({'stage_frame': i, 'frame_key': frame['frame_key'],
                           'decoded_members': visit_hashes})
        stats = {'cold_complete_members_decoded': len(bundle['members']), 'visits': visits,
                 'order_independent': True, 'model_load_count': 0, 'recomputed': False}
    else:
        # Pass 1 freezes whole-shot statistics from ALL exact raw + validity.
        lo, hi, count = float('inf'), -float('inf'), 0
        per_frame = []
        for f in params['frames']:
            raw = load(f['depth'], hw, np.float32)
            valid = load(f['validity'], hw, np.bool_)
            need(valid.any() and np.isfinite(raw[valid]).all() and (raw[valid] > 0).all(),
                 'CG valid axial Z must be nonempty, finite and positive')
            values = raw[valid]
            a, z = float(values.min()), float(values.max())
            lo, hi, count = min(lo, a), max(hi, z), count + int(valid.sum())
            per_frame.append({'stage_frame': f['stage_frame'], 'min': a, 'max': z, 'valid_count': int(valid.sum())})
            del raw, valid, values
        statistics = {'method': 'all_valid_minmax', 'mapping': 'axial_identity', 'lower_value': lo,
                      'upper_value': hi, 'valid_count': count, 'frame_count': len(params['frames']),
                      'statistics_key': b['statistics_key'], 'source_domain': 'all raw depth and explicit validity members',
                      'normalization_applied': False, 'per_frame': per_frame}
        resumed = {entry['frame']['stage_frame']: entry for entry in b.get('resumed_frames', [])}
        reused_count = 0
        # Pass 2 is bounded to one full frame, no temporal carry or previous-mask reuse.
        for f in params['frames']:
            checkpoint()
            i = f['stage_frame']
            folder = target / ('f%03d' % i)
            folder.mkdir(exist_ok=False)
            begin = len(members)
            if i in resumed:
                entry = resumed[i]
                for old in entry['members']:
                    checkpoint()
                    path = folder / Path(old['file']).name
                    need(protected.get(os.path.normcase(os.path.abspath(old['file']))) == old['sha256'], 'Unprotected resume member')
                    with Path(old['file']).open('rb') as source, path.open('xb') as dest:
                        for block in iter(lambda: source.read(1048576), b''):
                            checkpoint()
                            dest.write(block)
                    need(sha(path) == old['sha256'], 'Copied resume bytes differ')
                    if old['role'] == 'linear_straight_exr':
                        x, _ = color.read_linear_straight_exr(io, path, domain)
                    elif old['role'] == 'display_srgb_png':
                        x, _ = color.read(io, path, domain, 'png', 4)
                    else:
                        x = io.read_npy(path, tuple(old['shape']), (np.dtype(old['dtype']),))
                    record(path, old['role'], x, i)
                frame = dict(entry['frame'], reused_complete_frame=True)
                frame['member_sha256'] = [r['sha256'] for r in members[begin:]]
                frames.append(frame)
                publish_checkpoint(folder / 'frame-complete.json', {'status': 'partial_frame_complete', 'consumer_eligible': False,
                    'shot_key': b['shot_key'], 'frame': frame, 'members': members[begin:]})
                reused_count += 1
                continue
            rgba = load(f['rgb'], hw + (4,), np.float32)
            raw = load(f['depth'], hw, np.float32)
            valid = load(f['validity'], hw, np.bool_)
            imported = load(f['normal'], hw + (3,), np.float32)
            normal_valid = load(f['normal_validity'], hw, np.bool_)
            need(np.isfinite(rgba).all() and ((rgba[..., 3] >= 0) & (rgba[..., 3] <= 1)).all(), 'Finite straight RGBA/Alpha required')
            oracle = {'depth_prepare_version': '1.1', 'domain': domain,
                      'normal': {'method': 'import_geometry'}, 'validity': {'kind': 'explicit_mask'},
                      'semantics': {'measure': 'axial_z', 'near_order': 'smaller_nearer', 'units': 'scene_unit'}}
            io.validate_imported_normal(imported, normal_valid, valid, oracle)
            rotation = np.asarray(f['camera_to_world'], dtype=np.float64)[:3, :3]
            need(np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6, rtol=0) and
                 abs(float(np.linalg.det(rotation)) - 1) <= 1e-6, 'Camera transform must be rigid and right handed')
            derived = math.prepare_arrays(raw, valid, {'domain': domain, 'budget': params['budget'],
                'semantics': oracle['semantics'], 'normalization': {'method': 'none'}, 'edits': [],
                'normal': {'method': 'central_perspective', **f['K'], 'max_relative_jump': params['max_relative_jump'],
                           'boundary_exclusion': None}}, [])
            chosen, chosen_valid = (imported, normal_valid) if params['normal_source'] == 'import_geometry' else (derived['normal'], derived['normal_validity'])
            save(folder, 'raw.npy', 'raw', raw, i)
            save(folder, 'source-rgba.npy', 'source_rgba', rgba, i)
            for name, x in [('scalar', derived['scalar']), ('validity', valid), ('normal_imported', imported),
                            ('normal_imported_validity', normal_valid), ('normal_derived', derived['normal']),
                            ('normal_derived_validity', derived['normal_validity'])]:
                save(folder, name + '.npy', name, x, i)
            masks = []
            for kind in ('light', 'highlight', 'line'):
                d = f['masks'][kind]
                mask = np.full(hw, 0 if kind == 'line' else 1, np.float32) if d is None else load(d, hw, np.float32)
                need(np.isfinite(mask).all() and ((mask >= 0) & (mask <= 1)).all(), 'Mask must be finite float32 in 0..1')
                masks.append(mask)
                save(folder, kind + '-mask.npy', kind + '_mask', mask, i)
            t = time.perf_counter()
            output, delta, parts = kernel.render(rgba, derived['scalar'], chosen, valid, chosen_valid,
                                                  *masks, params['config'], kernel.Backend(np))
            kernel_seconds = time.perf_counter() - t
            need(np.isfinite(output).all() and np.array_equal(output[..., 3], rgba[..., 3]), 'Output finite/alpha invariant failed')
            save(folder, 'output.npy', 'output', output, i)
            save(folder, 'delta.npy', 'delta', delta, i)
            for j, part in enumerate(parts):
                for name in ('diffuse', 'specular', 'visibility', 'delta'):
                    save(folder, 'l%d-%s.npy' % (j, name), 'light:%s:%s' % (part['id'], name), part[name], i)
            exr = folder / 'output.exr'
            color.write_linear_straight_exr(io, exr, np.ascontiguousarray(output), domain, checkpoint)
            restored, _ = color.read_linear_straight_exr(io, exr, domain)
            need(np.array_equal(output, restored), 'EXR complete float32 readback differs')
            record(exr, 'linear_straight_exr', restored, i)
            png = folder / 'output.png'
            record(png, 'display_srgb_png', write_png(png, output), i)
            frames.append({'stage_frame': i, 'source_frame': f['source_frame'], 'scene_frame': f['scene_frame'],
                'time': f['time'], 'frame_key': b['frame_keys'][i], 'statistics_key': b['statistics_key'],
                'member_sha256': [r['sha256'] for r in members[begin:]], 'kernel_seconds': kernel_seconds,
                'derived_statistics': derived['statistics'], 'normal_source': params['normal_source']})
            # A checkpoint is evidence, never a consumer-eligible partial bundle.
            checkpoint_path = folder / 'frame-complete.json'
            publish_checkpoint(checkpoint_path, {'status': 'partial_frame_complete', 'consumer_eligible': False,
                'shot_key': b['shot_key'], 'frame': frames[-1], 'members': members[begin:]})
            del rgba, raw, valid, imported, normal_valid, derived, masks, output, delta, parts, restored
        stats = {'whole_shot': statistics, 'device': 'CPU_NUMPY', 'fallback': False,
                 'model_load_count': 0, 'max_resident_source_frames': 1, 'temporal_filter': 'none',
                 'temporal_quality_qualified': False, 'recovery_qualified': False,
                 'display_transform': 'Standard exact sRGB transfer; display RGB clamped only',
                 'reused_complete_frame_count': reused_count}
    for path, expected in protected.items():
        checkpoint()
        need(sha(path) == expected, 'Protected source changed', 'CONFLICT')
    checkpoint()
    report = {'schema': 'compositing-depth-sequence-worker-v1', 'status': 'passed', 'mode': b['mode'],
              'request_signature': b['request_signature'], 'members': members, 'frames': frames,
              'stats': stats, 'seconds': time.monotonic() - started, 'public_parent_success_required': True}
    with (target / 'worker-report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    return report


if __name__ == '__main__':
    job = Path(sys.argv[1])
    try:
        run(job)
    except BaseException as exc:
        path = job / 'depth/worker-report.json'
        code = getattr(exc, 'code', 'WORKER_FAILED')
        if not path.exists():
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'status': 'failed', 'error': {'code': code, 'message': str(exc)},
                           'partial_eligible': False}, stream, indent=2)
        raise SystemExit({'CANCELLED': 7, 'TIMEOUT': 6, 'CONFLICT': 4}.get(code, 8))
