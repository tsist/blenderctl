"""Strict depth numerical containers. No scene, model, network or conversion."""
import ast
import hashlib
from pathlib import Path
import struct
import zlib

import numpy as np
import OpenImageIO as oiio


class DepthIOError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def need(value, message, code='INVALID_DATA'):
    if not value:
        raise DepthIOError(code, message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def _path(desc):
    need(type(desc) is dict and type(desc.get('file')) is str and type(desc.get('expected_sha256')) is str,
         'File/SHA descriptor required')
    path = Path(desc['file'])
    need(path.is_absolute() and path.is_file(), 'Absolute existing input required')
    digest = desc['expected_sha256']
    need(len(digest) == 64 and all(c in '0123456789abcdef' for c in digest), 'Invalid SHA')
    need(sha(path) == digest, 'Input SHA mismatch', 'INPUT_CHANGED')
    return path


def _shape(domain):
    need(type(domain) is dict and all(type(domain.get(k)) is int and 4 <= domain[k] <= 2048 for k in ('height', 'width')),
         'Bounded integer image dimensions required')
    return (domain['height'], domain['width'])


def read_npy(path, shape, dtypes):
    """Read one complete NPY. Validate header before allocation, not after cast."""
    path = Path(path)
    with path.open('rb') as stream:
        try:
            version = np.lib.format.read_magic(stream)
            need(version in ((1, 0), (2, 0), (3, 0)), 'Unsupported NPY version')
            if version == (1, 0):
                header_shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=10000)
            elif version == (2, 0):
                header_shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=10000)
            else:
                length_bytes = stream.read(4)
                need(len(length_bytes) == 4, 'Truncated NPY3 header length')
                length = struct.unpack('<I', length_bytes)[0]
                need(0 < length <= 10000, 'NPY3 header exceeds bound')
                text = stream.read(length)
                need(len(text) == length, 'Truncated NPY3 header')
                header = ast.literal_eval(text.decode('utf-8'))
                need(type(header) is dict and set(header) == {'descr', 'fortran_order', 'shape'}
                     and type(header['fortran_order']) is bool and type(header['shape']) is tuple,
                     'NPY3 header structure differs')
                header_shape, fortran, dtype = header['shape'], header['fortran_order'], np.dtype(header['descr'])
            need(header_shape == tuple(shape) and not fortran and dtype in tuple(np.dtype(x) for x in dtypes),
                 'NPY actual dtype/shape/C header differs')
            expected_end = stream.tell() + int(np.prod(shape)) * dtype.itemsize
            need(path.stat().st_size == expected_end, 'NPY trailing/truncated data')
            stream.seek(0)
            array = np.load(stream, allow_pickle=False, max_header_size=10000)
            need(isinstance(array, np.ndarray) and array.shape == tuple(shape) and array.dtype == dtype
                 and array.flags.c_contiguous and stream.read(1) == b'', 'Complete NPY decode failed')
            return array
        except (ValueError, OSError, EOFError, SyntaxError, UnicodeError) as exc:
            raise DepthIOError('INVALID_DATA', 'NPY decode failed: ' + str(exc)) from exc


def _image(path, shape, container, channels=None, dtype=None):
    inp = oiio.ImageInput.open(str(path))
    need(inp is not None, 'Image could not open')
    try:
        spec = inp.spec()
        need(inp.format_name() == container and spec.width == shape[1] and spec.height == shape[0]
             and spec.depth == 1 and spec.x == spec.y == spec.z == 0
             and spec.full_width == shape[1] and spec.full_height == shape[0]
             and spec.full_x == spec.full_y == 0, 'Container/data/full-window domain differs')
        names = list(spec.channelnames)
        formats = list(spec.channelformats) or [spec.format] * spec.nchannels
        need(channels is None or names == list(channels), 'Image channels differ')
        need(dtype is None or all(str(fmt) == dtype for fmt in formats), 'Actual image precision differs; conversion forbidden')
        array = inp.read_image(format=oiio.FLOAT if dtype == 'float' else oiio.UINT8)
        need(array is not None, 'Full pixel decode failed')
        metadata = {'container': container, 'channels': names, 'actual_channel_formats': [str(f) for f in formats],
                    'shape': list(array.shape), 'dtype': str(array.dtype), 'data_window': [spec.x, spec.y, spec.width, spec.height],
                    'full_window': [spec.full_x, spec.full_y, spec.full_width, spec.full_height], 'part_count': 1}
        need(not inp.seek_subimage(1, 0), 'Multipart/multiimage input unsupported')
        return np.ascontiguousarray(array), metadata
    finally:
        inp.close()


def validate_imported_normal(normal, normal_validity, depth_valid, params):
    """Check original imported bytes without rotation, derivation or normalization."""
    need(type(params) is dict and params.get('depth_prepare_version') == '1.1'
         and params.get('normal', {}).get('method') == 'import_geometry'
         and params.get('semantics') == {'measure': 'axial_z', 'near_order': 'smaller_nearer', 'units': 'scene_unit'}
         and params.get('validity', {}).get('kind') == 'explicit_mask', 'Imported Normal v1.1 axial/explicit contract required')
    shape = _shape(params['domain'])
    need(isinstance(normal, np.ndarray) and normal.dtype == np.float32 and normal.shape == shape + (3,)
         and normal.flags.c_contiguous and bool(np.isfinite(normal).all()), 'Imported finite float32 C/HW3 required')
    for mask in (normal_validity, depth_valid):
        need(isinstance(mask, np.ndarray) and mask.shape == shape and mask.dtype in (np.dtype('bool'), np.dtype('float32'))
             and bool(np.isfinite(mask).all()) and bool(np.isin(mask, [0, 1]).all()), 'Imported validity must be finite binary bool or float32 HW')
    nv = np.array(normal_validity, dtype=bool, order='C', copy=True)
    valid = np.array(depth_valid, dtype=bool, order='C', copy=True)
    need(bool(nv.any()) and not bool((nv & ~valid).any()), 'Imported nonempty Normal validity must be depth-valid subset')
    need(bool((normal[~nv] == 0).all()), 'Imported invalid Normal must be exact zero vector')
    error = np.abs(np.linalg.norm(normal[nv].astype(np.float64), axis=1) - 1)
    need(bool((error <= 1e-5).all()), 'Imported unit length differs; implicit normalization forbidden')
    return nv


def read_imported_normal(desc, domain):
    """Decode declared signed Normal bytes; no color decode, reshape or cast."""
    need(type(desc) is dict and set(desc) == {'file', 'expected_sha256', 'format', 'channel'}
         and desc['format'] == 'NPY' and desc['channel'] is None, 'Imported Normal requires exact NPY descriptor')
    path = _path(desc)
    shape = _shape(domain) + (3,)
    array = read_npy(path, shape, ('float32',))
    need(bool(np.isfinite(array).all()), 'Imported Normal must be finite throughout, including invalid pixels')
    metadata = {'container': 'npy', 'channels': None, 'shape': list(shape), 'dtype': str(array.dtype),
                'complete_no_trailing': True, 'kind': 'declared_imported_normal', 'file': str(path),
                'sha256': desc['expected_sha256'], 'bytes': path.stat().st_size,
                'encoding': 'signed-float32-XYZ', 'source_conversion': False, 'producer_verified': False,
                'unit_and_validity_validation': 'requires independent normal-validity in numerical prepare'}
    need(sha(path) == desc['expected_sha256'], 'Input changed during Normal decode', 'INPUT_CHANGED')
    return array, metadata


def read_scalar(desc, domain, kind='scalar'):
    need(kind in ('scalar', 'validity', 'boundary', 'binary', 'edit'), 'Unknown scalar kind')
    path = _path(desc)
    shape = _shape(domain)
    fmt = desc.get('format')
    if fmt == 'NPY':
        need(desc.get('channel') is None, 'NPY channel must be null')
        types = ('float32', 'bool') if kind in ('validity', 'boundary', 'binary') else ('float32',)
        array = read_npy(path, shape, types)
        metadata = {'container': 'npy', 'channels': None, 'shape': list(shape), 'dtype': str(array.dtype), 'complete_no_trailing': True}
    elif fmt == 'EXR':
        array, metadata = _image(path, shape, 'openexr', dtype='float')
        need(array.shape == shape + (1,) and len(metadata['channels']) == 1, 'EXR must be single scalar channel')
        need(desc.get('channel') is None or type(desc['channel']) is str and desc['channel'] == metadata['channels'][0], 'Declared EXR channel differs')
        need(array.dtype == np.float32, 'Decoded EXR must be actual float32')
        array = np.ascontiguousarray(array[:, :, 0])
        metadata['shape'] = list(shape)
    else:
        raise DepthIOError('INVALID_DATA', 'Scalar format must be NPY or EXR')
    if kind in ('validity', 'boundary', 'binary'):
        need(bool(np.isfinite(array).all()) and bool(np.isin(array, [0, 1]).all()), 'Binary mask must be finite exact 0/1')
    elif kind == 'edit':
        need(array.dtype == np.float32 and bool(np.isfinite(array).all()) and bool(((array >= 0) & (array <= 1)).all()),
             'Edit mask must be float32 finite in [0,1]')
    metadata.update(kind=kind, file=str(path), sha256=desc['expected_sha256'], bytes=path.stat().st_size,
                    finite_count=int(np.isfinite(array).sum()), nonfinite_count=int((~np.isfinite(array)).sum()), source_conversion=False)
    need(sha(path) == desc['expected_sha256'], 'Input changed during scalar decode', 'INPUT_CHANGED')
    return array, metadata


def read_png(path, domain, channels):
    """Strict PNG8 read, also used for exact diagnostic-preview readback."""
    path = Path(path)
    with path.open('rb') as stream:
        header = stream.read(33)
    need(len(header) == 33 and header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR', 'PNG IHDR required')
    w, h, bits, colour, compression, filt, interlace = struct.unpack('>IIBBBBB', header[16:29])
    need(bits == 8 and colour == {1: 0, 3: 2, 4: 6}[channels] and compression == filt == 0 and interlace in (0, 1),
         'Only actual PNG8 gray/RGB/RGBA supported')
    shape = _shape(domain)
    need((h, w) == shape, 'PNG IHDR size differs')
    # OIIO can ignore data after IEND. Check the whole original container and
    # every CRC before pixel decode rather than accepting a valid prefix.
    with path.open('rb') as stream:
        need(stream.read(8) == b'\x89PNG\r\n\x1a\n', 'PNG signature differs')
        saw_idat = False
        first = True
        while True:
            head = stream.read(8)
            need(len(head) == 8, 'PNG truncated before IEND')
            length, tag = struct.unpack('>I4s', head)
            need(length <= path.stat().st_size - stream.tell() - 4, 'PNG chunk extends beyond file')
            need(not first or tag == b'IHDR' and length == 13, 'PNG first chunk must be IHDR')
            need(first or tag != b'IHDR', 'Duplicate PNG IHDR')
            need(tag not in (b'acTL', b'fcTL', b'fdAT'), 'Animated PNG unsupported')
            first = False
            checksum = zlib.crc32(tag)
            remaining = length
            while remaining:
                block = stream.read(min(1048576, remaining))
                need(bool(block), 'PNG truncated payload')
                checksum = zlib.crc32(block, checksum)
                remaining -= len(block)
            crc = stream.read(4)
            need(len(crc) == 4 and struct.unpack('>I', crc)[0] == checksum & 0xffffffff, 'PNG chunk CRC differs')
            saw_idat |= tag == b'IDAT'
            if tag == b'IEND':
                need(length == 0 and saw_idat and stream.read(1) == b'', 'PNG missing image data or trailing data')
                break
    names = {1: ['Y'], 3: ['R', 'G', 'B'], 4: ['R', 'G', 'B', 'A']}[channels]
    array, meta = _image(path, shape, 'png', names, 'uint8')
    need(array.dtype == np.uint8 and array.shape == shape + (channels,), 'Actual PNG uint8 pixel shape differs')
    return array, meta


def read_rgb(desc, domain):
    path = _path(desc)
    with path.open('rb') as stream:
        header = stream.read(33)
    need(len(header) == 33 and header[25] in (2, 6), 'RGB8/RGBA8 PNG required')
    array, metadata = read_png(path, domain, 3 if header[25] == 2 else 4)
    metadata.update(file=str(path), sha256=desc['expected_sha256'], bytes=path.stat().st_size,
                    encoding='display_sRGB_PNG8', source_conversion=False, alpha_preserved=array.shape[2] == 4)
    need(sha(path) == desc['expected_sha256'], 'Input changed during RGB decode', 'INPUT_CHANGED')
    return array, metadata


def validate_derived(arrays, params):
    shape = _shape(params['domain'])
    need(type(arrays) is dict and all(k in arrays for k in ('scalar', 'validity', 'normal', 'normal_validity')), 'Derived arrays incomplete')
    scalar, valid, normal, nv = (arrays[k] for k in ('scalar', 'validity', 'normal', 'normal_validity'))
    need(isinstance(scalar, np.ndarray) and scalar.dtype == np.float32 and scalar.shape == shape and scalar.flags.c_contiguous,
         'Derived scalar float32 C/HW required')
    for mask in (valid, nv):
        need(isinstance(mask, np.ndarray) and mask.dtype == np.bool_ and mask.shape == shape and mask.flags.c_contiguous, 'Derived binary bool C/HW required')
    need(bool(valid.any()) and bool(np.isfinite(scalar).all()) and bool((scalar[~valid] == 0).all()), 'Derived valid/finite/invalid-zero policy differs')
    if params['semantics']['measure'] == 'axial_z':
        need(bool((scalar[valid] > 0).all()), 'Derived axial must remain positive')
    need(not bool((nv & ~valid).any()), 'Normal validity must be subset of scalar validity')
    values = scalar[valid].astype(np.float64)
    stats = {'valid_count': int(valid.sum()), 'invalid_count': int((~valid).sum()), 'scalar_min': float(values.min()),
             'scalar_max': float(values.max()), 'scalar_mean': float(values.mean()), 'normal_valid_count': int(nv.sum())}
    if params['normal']['method'] == 'none':
        need(normal is None and not nv.any(), 'Unexpected normal for method none')
        stats['normal_unit_length_max_error'] = None
        return stats
    need(isinstance(normal, np.ndarray) and normal.dtype == np.float32 and normal.shape == shape + (3,)
         and normal.flags.c_contiguous and bool(np.isfinite(normal).all()) and bool((normal[~nv] == 0).all()), 'Normal finite float32 C/HW3 and invalid zero required')
    if params['normal']['method'] == 'import_geometry':
        need(params.get('depth_prepare_version') == '1.1' and params['semantics']['measure'] == 'axial_z'
             and params['validity']['kind'] == 'explicit_mask', 'Imported Normal v1.1 axial/explicit domain required')
        # Direct source Normal has its own validity, including legal image borders.
        # Producer math qualifies any edited cross-neighborhood independently.
    else:
        need(not nv[0].any() and not nv[-1].any() and not nv[:, 0].any() and not nv[:, -1].any(), 'Normal border must be invalid')
        allowed = np.zeros(shape, dtype=bool)
        allowed[1:-1, 1:-1] = valid[1:-1, 1:-1] & valid[1:-1, :-2] & valid[1:-1, 2:] & valid[:-2, 1:-1] & valid[2:, 1:-1]
        need(not bool((nv & ~allowed).any()), 'Normal stencil crosses invalid scalar')
    if nv.any():
        error = np.abs(np.linalg.norm(normal[nv].astype(np.float64), axis=1) - 1)
        need(float(error.max()) <= 1e-5, 'Normal unit length differs from float32 tolerance')
        stats['normal_unit_length_max_error'] = float(error.max())
    else:
        stats['normal_unit_length_max_error'] = None
    return stats
