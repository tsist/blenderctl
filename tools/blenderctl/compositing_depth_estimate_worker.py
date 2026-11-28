"""Offline DAV2 worker in existing fixed venv. Never installs, downloads or edits RGB."""
from pathlib import Path
import hashlib
import json
import math
import os
import sys
import time
import types


class ModelError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def need(condition, message, code='VALIDATION_FAILED'):
    if not condition:
        raise ModelError(code, message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4194304), b''): h.update(block)
    return h.hexdigest()


def strict(data):
    def pairs(values):
        result = {}
        for k, v in values:
            need(k not in result, 'Duplicate JSON key', 'CONFLICT'); result[k] = v
        return result
    def constant(v): raise ModelError('CONFLICT', 'Nonfinite JSON:' + v)
    result = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    def finite(value):
        if type(value) is float: need(math.isfinite(value), 'Nonfinite JSON', 'CONFLICT')
        elif type(value) is dict:
            for v in value.values(): finite(v)
        elif type(value) is list:
            for v in value: finite(v)
    finite(result)
    return result


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def install_import_boundary(allowed_files, base_prefix, namespace_files=()):
    """Reject an untrusted module spec before its loader can execute any code.

    No-site boot has only stdlib finders. Guard PathFinder specs for both added
    import roots, including .pyd priority and namespace-package search folders.
    System stdlib/native driver trust remains a separately declared exclusion.
    """
    import importlib.machinery
    def canonical_path(path): return os.path.normcase(os.path.abspath(path))
    allowed = {canonical_path(p) for p in allowed_files}
    # Trusted .pyi-only packages (e.g. cv2.aruco) are namespace directories.
    # Their stubs authorize the directory, never a new executable module.
    namespace_roots = list(allowed_files) + [p for p in namespace_files if Path(p).suffix.lower()=='.pyi']
    allowed_dirs = {canonical_path(parent) for p in namespace_roots for parent in Path(p).parents}
    base = canonical_path(base_prefix)
    def standard(path):
        value = canonical_path(path)
        return (value==base or value.startswith(base+os.sep)) and 'site-packages' not in value.split(os.sep)
    class BoundaryFinder:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is None: return None
            origin = spec.origin
            if origin in ('built-in','frozen'): return spec
            if origin is None:
                locations = list(spec.submodule_search_locations or [])
                need(bool(locations) and all(canonical_path(p) in allowed_dirs or standard(p) for p in locations),
                     'Unqualified namespace import refused:' + fullname, 'UNSUPPORTED')
            else:
                need(canonical_path(origin) in allowed or standard(origin),
                     'Unqualified module import refused:' + fullname, 'UNSUPPORTED')
            return spec
    finder = BoundaryFinder()
    sys.meta_path.insert(0, finder)
    return finder


def run(job):
    started = time.monotonic(); times = {}
    job = Path(job).resolve(); out = job / 'model'
    need(out.is_dir() and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,
         'Worker requires -I -S -B and owned output')
    prefix = Path(sys.pycache_prefix or '').resolve()
    need(prefix.is_relative_to(out) and prefix != out and prefix.is_dir() and not any(prefix.iterdir()), 'Fresh empty pycache required')
    binding_path = out / 'binding.json'; binding = strict(binding_path.read_bytes())
    need(binding.get('schema') == 'public-dav2-worker-binding-v1', 'Binding version differs')
    p, profile = binding['params'], binding['profile']
    need(hashlib.sha256(canonical(p)).hexdigest() == binding['request_signature'], 'Request signature differs')
    timeout = binding['worker_timeout_seconds']
    def checkpoint():
        need(not (job / 'cancel.request').exists() and not (job / 'model.cancel.request').exists(), 'Model cancellation requested', 'CANCELLED')
        need(time.monotonic() - started <= timeout, 'Model worker timed out', 'TIMEOUT')
    checkpoint()
    need(sys.version_info[:3] == tuple(profile['python_version']) and
         Path(sys.executable).resolve() == Path(profile['python']['file']).resolve() and
         sha(sys.executable) == profile['python']['expected_sha256'], 'Actual fixed venv launcher/version differs', 'CONFLICT')
    # -S prevents startup .pth/sitecustomize execution. Dependency path is explicit;
    # sys.prefix under Python3.13/-S may be base_prefix, and is never falsified.
    site = Path(profile['venv_root']) / 'Lib/site-packages'
    need(site.is_dir() and not any('site-packages' in x.lower() for x in sys.path), 'Unexpected preloaded dependency path', 'CONFLICT')
    sys.path.insert(0, str(site))
    def offline(event, args):
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'urllib.Request', 'http.client.connect'):
            raise ModelError('UNSUPPORTED', 'Network forbidden in local depth inference')
    sys.addaudithook(offline)
    for r in binding['codes']:
        checkpoint(); need(sha(r['file']) == r['sha256'], 'Frozen worker/source changed', 'CONFLICT')
    inventory_d = binding['guarded_input_inventory']
    need(sha(inventory_d['file'])==inventory_d['sha256'],'Guarded import inventory changed','CONFLICT')
    inventory = strict(Path(inventory_d['file']).read_bytes())
    allowed = [r['file'] for r in inventory['files'] if Path(r['file']).suffix.lower() in ('.py','.pyd','.dll')]
    stubs = [r['file'] for r in inventory['files'] if Path(r['file']).suffix.lower()=='.pyi']
    install_import_boundary(allowed, sys.base_prefix, stubs)
    import importlib.metadata as md
    actual = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in md.distributions(path=[str(site)])}
    required = {k.lower().replace('_', '-'): v for k, v in profile['package_versions'].items()}
    need(all(actual.get(k) == v for k,v in required.items()) and
         set(actual) == set(required) | {'pip'} and actual.get('pip') == '26.1.2', 'Fixed14 plus excluded seed pip versions differ', 'CONFLICT')
    times['stdlib_preimport_seconds'] = time.monotonic() - started
    t = time.monotonic()
    import numpy as np
    import cv2
    import torch
    import torchvision
    times['third_party_import_seconds'] = time.monotonic() - t
    for module in (np, cv2, torch, torchvision):
        need(Path(module.__file__).resolve().is_relative_to(site), 'Third party module outside fixed site', 'CONFLICT')
    checkpoint()
    need(torch.__version__ == profile['runtime']['torch'] and torchvision.__version__ == profile['runtime']['torchvision'] and
         torch.version.cuda == profile['runtime']['CUDA'] and torch.cuda.is_available(), 'Fixed CUDA runtime unavailable; no fallback', 'UNSUPPORTED')
    torch.cuda.set_device(0); device = torch.device('cuda:0')
    need(list(torch.cuda.get_device_capability(0)) == profile['gpu']['capability'] and
         torch.cuda.get_device_name(0) == profile['gpu']['name'], 'Fixed device identity differs', 'UNSUPPORTED')
    torch.backends.cuda.matmul.allow_tf32 = False; torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False; torch.manual_seed(0)
    torch.cuda.synchronize(device); torch.cuda.reset_peak_memory_stats(device)
    memory_before = torch.cuda.mem_get_info(device)
    t = time.monotonic()
    original = cv2.imread(p['rgb']['file'], cv2.IMREAD_UNCHANGED)
    h, w = p['domain']['height'], p['domain']['width']
    need(original is not None and original.dtype == np.uint8 and original.shape in ((h,w,3),(h,w,4)), 'Actual RGB8/RGBA8 shape differs')
    if original.shape[2] == 4: need(bool((original[:,:,3] == 255).all()), 'Nonopaque Alpha refused')
    bgr = np.ascontiguousarray(original[:,:,:3]); rgb = np.ascontiguousarray(bgr[:,:,::-1])
    need(hashlib.sha256(rgb.tobytes()).hexdigest() == binding['decoded_RGB_sha256'], 'Bundled/OpenCV channel decode differs', 'CONFLICT')
    times['RGB_decode_seconds'] = time.monotonic() - t
    checkpoint()
    sys.path.insert(0, profile['source_root'])
    t = time.monotonic()
    from depth_anything_v2.dpt import DepthAnythingV2
    from depth_anything_v2.dinov2_layers import attention
    need(attention.XFORMERS_AVAILABLE is False, 'Unqualified xformers backend', 'UNSUPPORTED')
    model = DepthAnythingV2(encoder='vitl', features=256, out_channels=[256,512,1024,1024])
    times['model_construct_CPU_seconds'] = time.monotonic() - t
    checkpoint(); t = time.monotonic()
    state = torch.load(profile['model']['file'], map_location='cpu', weights_only=True)
    model.load_state_dict(state, strict=True); del state
    times['strict_checkpoint_load_seconds'] = time.monotonic() - t
    checkpoint(); t = time.monotonic()
    model = model.to(device=device, dtype=torch.float32).eval(); torch.cuda.synchronize(device)
    times['model_to_CUDA_seconds'] = time.monotonic() - t
    checkpoint()
    with torch.no_grad(), torch.autocast(device_type='cuda', enabled=False):
        t = time.monotonic(); tensor, (height, width) = model.image2tensor(bgr, input_size=518); torch.cuda.synchronize(device)
        need(tensor.device == device and tensor.dtype == torch.float32 and (height,width) == (h,w) and
             list(tensor.shape) == binding['expected_tensor_shape'], 'Official preprocessing/device/domain differs')
        need(tensor.shape[-1]*tensor.shape[-2] <= p['budget']['max_tensor_pixels'], 'Actual tensor budget exceeded', 'RESOURCE_LIMIT')
        times['official_preprocess_seconds'] = time.monotonic() - t
        checkpoint(); t = time.monotonic(); native = model.forward(tensor); torch.cuda.synchronize(device)
        native_shape = list(native.shape); times['forward_seconds'] = time.monotonic() - t
        checkpoint(); t = time.monotonic()
        recovered = torch.nn.functional.interpolate(native[:,None], (h,w), mode='bilinear', align_corners=True)[0,0]
        torch.cuda.synchronize(device); times['official_recover_seconds'] = time.monotonic() - t
        checkpoint(); depth = recovered.cpu().numpy()
    need(depth.dtype == np.float32 and depth.shape == (h,w) and depth.flags.c_contiguous and bool(np.isfinite(depth).all()),
         'Raw depth is not complete finite float32 C/HW; no cleaning/filling')
    checkpoint(); raw = out / 'raw-depth.npy'
    with raw.open('xb') as stream: np.save(stream, depth, allow_pickle=False)
    with raw.open('rb') as stream:
        restored = np.load(stream, allow_pickle=False)
        need(stream.read(1) == b'' and restored.dtype == np.float32 and restored.shape == (h,w) and
             restored.flags.c_contiguous and np.array_equal(depth, restored), 'Full raw NPY roundtrip differs')
    statistics = {'min': float(depth.min()), 'max': float(depth.max()), 'mean': float(depth.mean()),
                  'finite_count': int(np.isfinite(depth).sum()), 'negative_count': int((depth<0).sum()),
                  'finite_is_confidence': False, 'constant': bool(float(depth.max()) == float(depth.min()))}
    display = np.zeros((h,w), np.uint8) if statistics['constant'] else np.clip((depth-statistics['min'])/(statistics['max']-statistics['min'])*255,0,255).astype(np.uint8)
    encoded, pixels = cv2.imencode('.png', display); need(encoded, 'Diagnostic PNG encode failed')
    path = out / 'depth-display.png'
    with path.open('xb') as stream: stream.write(pixels.tobytes())
    checkpoint()
    times['worker_total_seconds'] = time.monotonic() - started
    report = {'schema': 'public-dav2-numeric-worker-v1', 'status': 'passed', 'request_signature': binding['request_signature'],
              'raw': {'file': str(raw), 'sha256': sha(raw), 'bytes': raw.stat().st_size, 'dtype': 'float32', 'shape': [h,w], 'layout': 'C-HW'},
              'display': {'file': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size, 'consumer_data': False},
              'statistics': statistics,
              'preprocess': {'input_size': 518, 'tensor_shape': list(tensor.shape), 'native_depth_shape': native_shape,
                             'resize': 'official lower_bound aspect/multiple14/OpenCV INTER_CUBIC',
                             'recover': 'PyTorch bilinear align_corners=True', 'output_shape': [h,w], 'crop': 'none', 'pad': 'none'},
              'runtime': {'python': sys.version, 'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
                          'no_site': bool(sys.flags.no_site), 'explicit_site': str(site), 'isolated': True, 'no_existing_pyc': True,
                          'torch': str(torch.__version__), 'torchvision': str(torchvision.__version__), 'CUDA': torch.version.cuda,
                          'actual_device': str(device), 'device_name': torch.cuda.get_device_name(0), 'capability': list(torch.cuda.get_device_capability(0)),
                          'dtype': 'float32', 'autocast': False, 'TF32': False, 'fallback': False},
              'colour': {'source': 'display_sRGB_PNG8', 'alpha': 'require_opaque; original unchanged, model uses first3',
                         'decode': 'cv2 BGR then official RGB/divide255/ImageNet mean/std', 'inverse_gamma': False, 'source_is_albedo': False},
              'memory': {'peak_allocated_bytes': int(torch.cuda.max_memory_allocated(device)),
                         'peak_reserved_bytes': int(torch.cuda.max_memory_reserved(device)), 'free_total_before': list(memory_before),
                         'scope': 'Torch allocator, not full-system peak'}, 'timing_seconds': times,
              'metric_depth_proven': False, 'calibration_proven': False, 'consumer_eligible': False}
    with (out/'worker-report.json').open('x', encoding='utf-8') as stream: json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    checkpoint()
    return report


def main():
    need(len(sys.argv) == 2, 'One owned job path required')
    job = Path(sys.argv[1])
    try:
        result = run(job)
        print(json.dumps({'status': 'passed', 'raw': result['raw'], 'forward_seconds': result['timing_seconds']['forward_seconds']}))
        return 0
    except Exception as error:
        code = getattr(error, 'code', 'RESOURCE_LIMIT' if 'OutOfMemory' in type(error).__name__ else 'WORKER_FAILED')
        allowed = {'VALIDATION_FAILED':8, 'CONFLICT':4, 'UNSUPPORTED':10, 'TIMEOUT':6, 'CANCELLED':7, 'IO_ERROR':9, 'RESOURCE_LIMIT':11, 'WORKER_FAILED':5}
        code = code if code in allowed else 'WORKER_FAILED'
        report = {'status': 'failed', 'error': {'code': code, 'type': type(error).__name__, 'message': str(error)}, 'consumer_eligible': False}
        try:
            target = job/'model/worker-report.json'
            if job.is_absolute() and target.parent.is_dir() and not target.exists():
                with target.open('x', encoding='utf-8') as stream: json.dump(report, stream, indent=2, allow_nan=False)
        except Exception as secondary: report['secondary_error'] = str(secondary)
        try: print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        except Exception: pass
        return allowed[code]


if __name__ == '__main__': raise SystemExit(main())
