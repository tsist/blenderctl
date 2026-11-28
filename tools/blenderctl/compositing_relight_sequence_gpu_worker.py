"""Single owned no-site CUDA sequence worker; fixed qualified14/DAV2 only.

Host owns original/packed inputs, code, package/source/model guards for the whole
batch. This process imports once, loads the model at most once, estimates each
new RGB once, freezes exact whole-shot percentiles, then lights one frame at a
time. CPU OIIO work is performed by separate owned IO stages.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import types


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def need(value, message, code='VALIDATION_FAILED'):
    if not value:
        exc = RuntimeError(message)
        exc.code = code
        raise exc


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def run(job):
    target = job / 'depth'
    b = json.loads((target / 'binding.json').read_text(encoding='utf-8'))
    p, profile = b['params'], b['profile']
    started = time.monotonic()
    need(sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode, 'No-site isolated no-pyc required')
    need(sys.version_info[:3] == tuple(profile['python_version']) and
         sha(sys.executable) == profile['python']['expected_sha256'], 'Actual Python identity differs')
    need(Path(sys.pycache_prefix).is_dir() and not any(Path(sys.pycache_prefix).iterdir()), 'Empty pycache required')

    def checkpoint():
        need(not (job / 'depth.cancel.request').exists(), 'Sequence GPU cancelled', 'CANCELLED')
        need(time.monotonic() - started <= b['timeout_seconds'], 'Sequence GPU timed out', 'TIMEOUT')

    codes = {r['role']: r for r in b['codes']}
    for r in b['codes']:
        checkpoint()
        need(sha(r['file']) == r['sha256'], 'Frozen GPU source changed', 'CONFLICT')

    def pin(name, role):
        r = codes[role]
        data = Path(r['file']).read_bytes()
        need(hashlib.sha256(data).hexdigest() == r['sha256'], 'Pinned source differs')
        module = types.ModuleType(name)
        module.__file__ = r['file']
        exec(compile(data, r['file'], 'exec'), module.__dict__)
        return module
    need(sha(__file__) == codes['depth_worker']['sha256'], 'Actual GPU worker differs')
    ownership = pin('sequence_ownership', 'sequence:actual_owner')
    actual_worker = ownership.worker_gate(target, b['request_signature'])
    need(os.path.normcase(actual_worker['actual_exe']) == os.path.normcase(b['native_python']['file']) and
         sha(actual_worker['actual_exe']) == b['native_python']['expected_sha256'], 'Actual native worker differs')

    def offline(event, args):
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'urllib.Request', 'http.client.connect'):
            raise RuntimeError('Network forbidden')
    sys.addaudithook(offline)
    site = Path(profile['venv_root']) / 'Lib/site-packages'
    sys.path.insert(0, str(site))
    source = Path(codes['sequence:import_boundary']['file']).read_bytes()
    selected = [node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and
                node.name in ('ModelError', 'need', 'install_import_boundary')]
    scope = {'Path': Path, 'os': os, 'sys': sys}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'qualified_sequence_import_boundary', 'exec'), scope)
    inventory = b['inventory']
    need(sha(inventory['file']) == inventory['sha256'], 'Guard inventory changed')
    files = json.loads(Path(inventory['file']).read_text(encoding='utf-8'))
    scope['install_import_boundary']([r['file'] for r in files if Path(r['file']).suffix.lower() in ('.py', '.pyd', '.dll')],
        sys.base_prefix, [r['file'] for r in files if Path(r['file']).suffix.lower() == '.pyi'])
    import importlib.metadata as md
    actual = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in md.distributions(path=[str(site)])}
    required = {k.lower().replace('_', '-'): v for k, v in profile['package_versions'].items()}
    need(all(actual.get(k) == v for k, v in required.items()) and set(actual) == set(required) | {'pip'} and
         actual['pip'] == '26.1.2', 'Exact qualified14 versions differ')
    t = time.monotonic()
    import numpy as np
    import torch
    need(Path(np.__file__).resolve().is_relative_to(site) and Path(torch.__file__).resolve().is_relative_to(site), 'Unexpected module root')
    need(torch.__version__ == profile['runtime']['torch'] and torch.version.cuda == profile['runtime']['CUDA'] and
         torch.cuda.is_available(), 'CUDA runtime differs; no fallback', 'UNSUPPORTED')
    torch.cuda.set_device(0)
    need(torch.cuda.get_device_name(0) == profile['gpu']['name'] and
         list(torch.cuda.get_device_capability(0)) == profile['gpu']['capability'], 'Fixed CUDA0 device differs')
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(0)
    import_seconds = time.monotonic() - t
    kernel = pin('sequence_GPU_kernel', 'sequence:kernel')
    h, w = p['domain']['height'], p['domain']['width']
    hw = h, w
    packed = {(r['stage_frame'], r['role']): r for r in b['packed']}
    guarded = {os.path.normcase(os.path.abspath(r['file'])): r['sha256'] for r in b['input_files']}
    members, frames, raw_rows = [], [], []

    def decode_npy(r):
        shape = r['shape']
        need(shape in ([h, w], [h, w, 3], [h, w, 4]) and r['dtype'] in ('float32', 'bool', 'uint8'), 'Bounded NPY domain/type required')
        with Path(r['file']).open('rb') as stream:
            version = np.lib.format.read_magic(stream)
            need(version in ((1, 0), (2, 0)), 'Finite NPY1/2 required')
            actual_shape, fortran, dtype = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                                           else np.lib.format.read_array_header_2_0)(stream, max_header_size=10000)
            need(actual_shape == tuple(shape) and not fortran and str(dtype) == r['dtype'] and
                 stream.tell() + int(np.prod(shape)) * dtype.itemsize == Path(r['file']).stat().st_size,
                 'NPY header/complete byte extent differs')
            stream.seek(0)
            x = np.load(stream, allow_pickle=False, max_header_size=10000)
            need(stream.read(1) == b'' and x.flags.c_contiguous, 'NPY complete decode differs')
        return x

    def read_array(r):
        checkpoint()
        need(guarded.get(os.path.normcase(os.path.abspath(r['file']))) == r['sha256'] and sha(r['file']) == r['sha256'],
             'NPY is absent from guarded closure or changed', 'CONFLICT')
        x = decode_npy(r)
        need(x.shape == tuple(r['shape']) and str(x.dtype) == r['dtype'] and x.flags.c_contiguous, 'NPY actual domain/type differs')
        return x

    def read_owned(r):
        checkpoint()
        need(sha(r['file']) == r['sha256'], 'Owned raw changed', 'CONFLICT')
        x = decode_npy(r)
        need(x.shape == tuple(r['shape']) and str(x.dtype) == r['dtype'] and x.flags.c_contiguous, 'Owned raw domain/type differs')
        return x

    def save(folder, name, role, x, i):
        checkpoint()
        x = np.ascontiguousarray(x)
        path = folder / name
        with path.open('xb') as stream:
            np.save(stream, x, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        r = {'file': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size, 'role': role,
             'shape': list(x.shape), 'dtype': str(x.dtype), 'stage_frame': i}
        restored = read_owned(r)
        need(np.array_equal(x, restored), 'GPU NPY complete readback differs')
        members.append(r)
        return r

    def publish(path, value):
        partial = path.with_suffix('.partial')
        with partial.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(partial, path)

    cg = p['source']['kind'] == 'declared_CG_geometry_pass'
    model, load_count = None, 0
    resumed_raw = {r['stage_frame']: r for r in b.get('resumed_raw', [])}
    completed_raw = {r['stage_frame']: r for r in b.get('reused_raw', [])}
    need(not completed_raw or (not cg and not resumed_raw and
         sorted(completed_raw) == list(range(len(p['frames'])))), 'Completed raw reuse must cover whole RGB shot')
    resumed_raw.update(completed_raw)
    if not cg and len(resumed_raw) < len(p['frames']):
        import torchvision
        need(torchvision.__version__ == profile['runtime']['torchvision'], 'Fixed torchvision differs')
        sys.path.insert(0, profile['source_root'])
        from depth_anything_v2.dpt import DepthAnythingV2
        from depth_anything_v2.dinov2_layers import attention
        need(attention.XFORMERS_AVAILABLE is False, 'Unqualified xformers backend')
        model = DepthAnythingV2(encoder='vitl', features=256, out_channels=[256, 512, 1024, 1024])
        checkpoint()
        state = torch.load(profile['model']['file'], map_location='cpu', weights_only=True)
        model.load_state_dict(state, strict=True)
        del state
        model = model.to(device='cuda:0', dtype=torch.float32).eval()
        torch.cuda.synchronize()
        load_count = 1
    # Estimate/raw pass. All original raw outputs remain immutable thereafter.
    reused_frames = {entry['frame']['stage_frame']: entry for entry in b.get('resumed_frames', [])}
    reused_frame_count = 0
    for f in p['frames']:
        checkpoint()
        i = f['stage_frame']
        folder = target / ('f%03d' % i)
        folder.mkdir(exist_ok=False)
        if cg:
            raw_rows.append(packed[(i, 'raw')])
            continue
        if i in resumed_raw:
            raw = read_array(resumed_raw[i])
            inference = {'reused_raw': True, 'forward_executed': False,
                'completed_source_authority': b.get('raw_reuse_authority') if i in completed_raw else None,
                'source_raw': resumed_raw[i]}
        else:
            rgb = read_array(packed[(i, 'model_rgb8')])
            need(rgb.dtype == np.uint8 and rgb.shape == hw + (3,), 'Prepared RGB8 domain differs')
            bgr = np.ascontiguousarray(rgb[..., ::-1])
            with torch.no_grad(), torch.autocast(device_type='cuda', enabled=False):
                tensor, restored_shape = model.image2tensor(bgr, input_size=518)
                need(list(tensor.shape) == b['expected_tensor_shape'] and tuple(restored_shape) == hw and
                     tensor.dtype == torch.float32 and str(tensor.device) == 'cuda:0' and
                     tensor.shape[-2] * tensor.shape[-1] <= p['source']['max_tensor_pixels'],
                     'Official preprocessing/domain/tensor budget differs')
                native = model.forward(tensor)
                recovered = torch.nn.functional.interpolate(native[:, None], hw, mode='bilinear', align_corners=True)[0, 0]
                torch.cuda.synchronize()
                raw = recovered.cpu().numpy()
                inference = {'reused_raw': False, 'forward_executed': True,
                    'tensor_shape': list(tensor.shape), 'native_shape': list(native.shape), 'recover': 'bilinear align_corners=True'}
                del tensor, native, recovered
            del rgb, bgr
        need(raw.dtype == np.float32 and raw.shape == hw and raw.flags.c_contiguous and np.isfinite(raw).all(), 'Finite complete raw FP32 required')
        r = save(folder, 'raw.npy', 'raw', raw, i)
        if i in completed_raw:
            need(r['sha256'] == completed_raw[i]['sha256'], 'Reused raw encoding changed source bytes', 'CONFLICT')
        raw_rows.append(r)
        publish(folder / 'raw-complete.json', {'schema': 'sequence-raw-checkpoint-v1', 'shot_key': b['shot_key'],
            'statistics_key': b['statistics_key'], 'stage_frame': i, 'raw': r, 'inference': inference,
            'consumer_eligible': False})
        del raw
    if model is not None:
        del model
        torch.cuda.empty_cache()
    checkpoint()
    statistics_key, frame_keys = b['statistics_key'], b['frame_keys']
    if not cg:
        raw_fingerprints = [{'stage_frame': r['stage_frame'], 'sha256': r['sha256']} for r in raw_rows]
        statistics_key = signature({'dependency_key': statistics_key, 'raw_members': raw_fingerprints,
                                    'validity': 'all_frame_artistic_declared'})
        frame_keys = [signature({'dependency_frame_key': k, 'statistics_key': statistics_key}) for k in frame_keys]
    if cg:
        count, lo, hi = 0, float('inf'), -float('inf')
        for i, r in enumerate(raw_rows):
            raw = read_array(r)
            valid = read_array(packed[(i, 'validity')])
            need(valid.any() and np.isfinite(raw[valid]).all() and (raw[valid] > 0).all(), 'CG axial domain invalid')
            values = raw[valid]
            count += int(valid.sum())
            lo, hi = min(lo, float(values.min())), max(hi, float(values.max()))
        stats = {'method': 'all_valid_minmax', 'mapping': 'axial_identity', 'lower_value': lo, 'upper_value': hi,
                 'valid_count': count, 'normalization_applied': False}
    else:
        # Exact, whole-shot, pixel-weighted linear percentile. Disk-backed finite
        # scratch prevents keeping120 RGB/depth frames in RAM/GPU simultaneously.
        scratch = target / 'percentile-values.dat'
        values = np.memmap(scratch, dtype=np.float32, mode='w+', shape=(len(raw_rows) * h * w,))
        for i, r in enumerate(raw_rows):
            raw = read_owned(r)
            values[i*h*w:(i+1)*h*w] = raw.reshape(-1)
            del raw
        values.flush()
        checkpoint()
        lo, hi = [float(x) for x in np.percentile(values, [p['statistics']['low'], p['statistics']['high']],
                                                  method='linear', overwrite_input=True)]
        del values
        need(np.isfinite([lo, hi]).all() and hi > lo, 'Whole-shot percentile range is degenerate')
        stats = {'method': 'all_valid_exact_percentile', 'mapping': 'relative_near_one',
            'percentile_method': 'numpy linear, pixel weighted, entire shot; disk-backed scratch',
            'percentiles': [p['statistics']['low'], p['statistics']['high']], 'lower_value': lo, 'upper_value': hi,
            'valid_count': len(raw_rows)*h*w, 'normalization_applied': True, 'near_value': 1,
            'raw_model_drift_corrected': False}
    stats.update(statistics_key=statistics_key, frame_count=len(p['frames']),
                 statistics_dependency_key=b['statistics_key'])
    if not cg:
        stats['raw_members'] = raw_fingerprints
    publish(target / 'shot-statistics.json', stats)

    def relative_normal(scalar, valid):
        scale = p['config']['frame']['height_scale']
        z = scalar.astype(np.float64) * scale
        core = valid[1:-1, 1:-1].copy()
        for other in (z[1:-1, :-2], z[1:-1, 2:], z[:-2, 1:-1], z[2:, 1:-1]):
            core &= np.abs(other - z[1:-1, 1:-1]) <= p['max_relative_jump']
        dx = (z[1:-1, 2:] - z[1:-1, :-2]) * h / 2
        dy = -(z[2:, 1:-1] - z[:-2, 1:-1]) * h / 2
        v = np.stack((-dx, -dy, np.ones_like(dx)), axis=-1)
        length = np.linalg.norm(v, axis=-1)
        normal = np.zeros(hw + (3,), np.float32)
        normal[1:-1, 1:-1] = np.where(core[..., None], v / length[..., None], 0).astype(np.float32)
        nv = np.zeros(hw, np.bool_)
        nv[1:-1, 1:-1] = core
        return normal, nv

    # Lighting pass: finite one-frame tensor upload/readback; no previous frame state.
    for f in p['frames']:
        checkpoint()
        i = f['stage_frame']
        folder = target / ('f%03d' % i)
        begin = len(members)
        if i in reused_frames:
            entry = reused_frames[i]
            need(entry['frame']['frame_key'] == frame_keys[i] and entry['frame']['statistics_key'] == statistics_key,
                 'Realized raw/statistics changed; cached lighting frame is invalid', 'CONFLICT')
            for r in entry['members']:
                save(folder, Path(r['file']).name, r['role'], read_array(r), i)
            frame = dict(entry['frame'], reused_complete_frame=True)
            frames.append(frame)
            publish(folder / 'frame-complete.json', {'schema': 'sequence-frame-checkpoint-v1', 'shot_key': b['shot_key'],
                'frame': frame, 'members': members[begin:], 'consumer_eligible': False})
            reused_frame_count += 1
            continue
        rgba = read_array(packed[(i, 'source_rgba')])
        if cg:
            scalar = read_array(packed[(i, 'scalar')])
            valid = read_array(packed[(i, 'validity')])
            suffix = 'imported' if p['normal_source'] == 'import_geometry' else 'derived'
            normal = read_array(packed[(i, 'normal_' + suffix)])
            nv = read_array(packed[(i, 'normal_' + suffix + '_validity')])
        else:
            raw = read_owned(raw_rows[i])
            scalar = np.clip((raw.astype(np.float64) - lo) / (hi - lo), 0, 1).astype(np.float32)
            valid = np.ones(hw, np.bool_)
            normal, nv = relative_normal(scalar, valid)
            for role, x in [('scalar', scalar), ('validity', valid), ('normal_derived', normal), ('normal_derived_validity', nv)]:
                save(folder, role + '.npy', role, x, i)
            del raw
        masks = [read_array(packed[(i, name + '_mask')]) for name in ('light', 'highlight', 'line')]
        with torch.no_grad(), torch.autocast(device_type='cuda', enabled=False):
            tensors = [torch.from_numpy(x).to('cuda:0') for x in (rgba, scalar, normal, valid, nv, *masks)]
            torch.cuda.synchronize()
            t = time.perf_counter()
            output, delta, parts = kernel.render(*tensors, p['config'], kernel.Backend(torch, 'cuda:0'))
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - t
            out = output.cpu().numpy()
            need(np.array_equal(out[..., 3], rgba[..., 3]), 'GPU Alpha changed')
            save(folder, 'output.npy', 'output', out, i)
            save(folder, 'delta.npy', 'delta', delta.cpu().numpy(), i)
            for j, part in enumerate(parts):
                for name in ('diffuse', 'specular', 'visibility', 'delta'):
                    save(folder, 'l%d-%s.npy' % (j, name), 'light:%s:%s' % (part['id'], name), part[name].cpu().numpy(), i)
            del tensors, output, delta, parts, out
        frame = {'stage_frame': i, 'source_frame': f['source_frame'], 'scene_frame': f['scene_frame'], 'time': f['time'],
            'frame_key': frame_keys[i], 'statistics_key': statistics_key, 'kernel_seconds': elapsed,
            'normal_source': p['normal_source']}
        frames.append(frame)
        publish(folder / 'frame-complete.json', {'schema': 'sequence-frame-checkpoint-v1', 'shot_key': b['shot_key'],
            'frame': frame, 'members': members[begin:], 'consumer_eligible': False})
        del rgba, scalar, normal, valid, nv, masks
    checkpoint()
    for path, expected in guarded.items():
        checkpoint()
        need(sha(path) == expected, 'Guarded input changed after GPU sequence', 'CONFLICT')
    report = {'schema': 'compositing-depth-sequence-GPU-worker-v1', 'status': 'passed', 'mode': 'compute',
        'request_signature': b['request_signature'], 'members': members, 'frames': frames,
        'stats': {'whole_shot': stats, 'device': 'CUDA0_FP32', 'fallback': False, 'TF32': False, 'autocast': False,
            'model_load_count': load_count, 'model_executed': load_count > 0, 'reused_raw_count': len(resumed_raw),
            'completed_source_raw_reuse_count': len(completed_raw),
            'reused_complete_frame_count': reused_frame_count,
            'max_resident_source_frames': 1, 'third_party_import_seconds': import_seconds,
            'actual_worker': actual_worker, 'temporal_filter': 'none', 'temporal_quality_qualified': False,
            'metric_depth_declared': cg, 'metric_depth_proven': False, 'model_confidence_proven': False, 'source_is_albedo': False,
            'validity_is_confidence': False},
        'seconds': time.monotonic() - started, 'public_parent_success_required': True}
    publish(target / 'worker-report.json', report)
    return report


if __name__ == '__main__':
    job = Path(sys.argv[1])
    try:
        run(job)
    except BaseException as exc:
        code = getattr(exc, 'code', 'WORKER_FAILED')
        path = job / 'depth/worker-report.json'
        if not path.exists():
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'status': 'failed', 'error': {'code': code, 'message': str(exc)},
                           'partial_eligible': False}, stream, allow_nan=False)
        raise SystemExit({'CANCELLED': 7, 'TIMEOUT': 6, 'CONFLICT': 4}.get(code, 8))
