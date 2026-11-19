"""Saved-mesh adapter and guarded PNG publication for uv.grid-transfer."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import struct
import tempfile
import time
import zipfile
import zlib

import bpy
import numpy as np

from protocol import Failure, atomic_json
from uv_grid_transfer_contract import ATLAS_BUDGET, normalize
from uv_workflow import _guard_inputs, _open_source, _progress, _runtime_descriptor, descriptor
from uv_workflow_addon import grid_transfer


PNG = b'\x89PNG\r\n\x1a\n'
TANGENT_TOLERANCE_DEGREES = .001


def _static_mesh(obj, layer):
    if (obj is None or obj.type != 'MESH' or obj.library or obj.data.library
            or obj.override_library or obj.parent or obj.modifiers or obj.constraints
            or obj.animation_data or obj.data.animation_data or obj.data.shape_keys):
        raise Failure('UNSUPPORTED', 'Grid transfer requires standalone static local meshes')
    mesh = obj.data
    matrix = np.array(obj.matrix_basis, dtype=np.float64)
    if not np.isfinite(matrix).all() or np.linalg.det(matrix[:3, :3]) <= 0:
        raise Failure('UNSUPPORTED', 'Grid transfer requires a positive finite object transform')
    uv = mesh.uv_layers.get(layer)
    if uv is None:
        raise Failure('NOT_FOUND', 'Missing saved UV layer: ' + obj.name + '/' + layer)
    count = len(mesh.polygons)
    if not count or len(mesh.loops) != count * 3:
        raise Failure('UNSUPPORTED', 'Grid transfer requires explicit triangular faces: ' + obj.name)
    sizes, starts = (np.empty(count, dtype=np.int32) for _ in range(2))
    mesh.polygons.foreach_get('loop_total', sizes)
    mesh.polygons.foreach_get('loop_start', starts)
    if np.any(sizes != 3) or not np.array_equal(starts, 3 * np.arange(count)):
        raise Failure('UNSUPPORTED', 'Grid transfer requires exact ordered triangle corners: ' + obj.name)
    vertices = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    edges = np.empty((len(mesh.edges), 2), dtype=np.int32)
    corners = np.empty(len(mesh.loops), dtype=np.int32)
    uv_coords = np.empty((count, 3, 2), dtype=np.float32)
    mesh.vertices.foreach_get('co', vertices.ravel())
    mesh.edges.foreach_get('vertices', edges.ravel())
    mesh.loops.foreach_get('vertex_index', corners)
    uv.uv.foreach_get('vector', uv_coords.ravel())

    normals = np.empty((len(mesh.loops), 3), dtype=np.float32)
    tangents = np.empty_like(normals)
    hands = np.empty(len(mesh.loops), dtype=np.float32)
    mesh.calc_tangents(uvmap=layer)
    mesh.corner_normals.foreach_get('vector', normals.ravel())
    mesh.loops.foreach_get('tangent', tangents.ravel())
    mesh.loops.foreach_get('bitangent_sign', hands)
    mesh.free_tangents()
    return uv_coords, (matrix, vertices, edges, corners, normals, hands), tangents


def _read_geometry(params):
    spec = params['manifest']
    scene = bpy.data.scenes.get(spec['source_scene'])
    if scene is None:
        raise Failure('NOT_FOUND', 'Missing saved source scene: ' + spec['source_scene'])
    bpy.context.window.scene = scene
    bpy.context.view_layer.update()
    objects = spec['objects']
    sources = [scene.objects.get(row['source_object']) for row in objects]
    names = [row['destination_object'] for row in objects]
    with bpy.data.libraries.load(params['destination']['file'], link=False) as (catalog, loaded):
        if any(name not in catalog.objects for name in names):
            raise Failure('NOT_FOUND', 'Destination BLEND does not contain the declared objects')
        loaded.objects = list(names)
    src_triangles, dst_triangles, patches, bases = [], [], [], []
    for row, source, destination in zip(objects, sources, loaded.objects, strict=True):
        old_uv, old_shape, old_t = _static_mesh(source, row['source_uv'])
        new_uv, new_shape, new_t = _static_mesh(destination, row['destination_uv'])
        label = row['source_object']
        if (not np.isfinite(old_shape[1]).all()
                or not np.isfinite(old_shape[4]).all()
                or not all(np.array_equal(a, b) for a, b in zip(old_shape[:-1], new_shape[:-1], strict=True))):
            raise Failure('VALIDATION_FAILED', 'Grid source and destination geometry, transform or corner normals differ: ' + label)
        if len(old_uv) != len(row['patch_ids']):
            raise Failure('VALIDATION_FAILED', 'Patch assignments must cover all saved triangles: ' + label)
        left, right = old_t.astype(np.float64), new_t.astype(np.float64)
        sizes = np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1)
        worst = None
        if np.isfinite(sizes).all() and np.all(sizes >= .9):
            angles = np.degrees(np.arctan2(np.linalg.norm(np.cross(left, right), axis=1),
                                           np.einsum('ij,ij->i', left, right)))
            worst = float(angles.max())
        hands_match = np.isin(old_shape[5], (-1, 1)).all() and np.array_equal(old_shape[5], new_shape[5])
        bases.append({'source_object': label, 'destination_object': row['destination_object'],
                      'source_uv': row['source_uv'], 'destination_uv': row['destination_uv'],
                      'triangles': len(old_uv), 'corners': len(old_t),
                      'source_destination_normal_and_handedness_exact': bool(hands_match),
                      'max_tangent_angle_degrees': worst})
        src_triangles.append(old_uv)
        dst_triangles.append(new_uv)
        patches.append(np.asarray(row['patch_ids'], dtype=np.int32))
    return (np.concatenate(src_triangles).astype(np.float64) * spec['source_size'],
            np.concatenate(dst_triangles).astype(np.float64) * spec['destination_size'],
            np.concatenate(patches), bases)


def _pixels(image, size):
    width, height = size
    if (tuple(image.size) != tuple(size) or image.channels != 4 or image.depth != 32
            or image.is_float or len(image.pixels) != width * height * 4):
        raise Failure('UNSUPPORTED', 'Grid transfer requires byte RGBA at the declared size')
    raw = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(raw)
    raw = raw.astype(np.float64) * 255
    code = np.rint(raw)
    if (not np.isfinite(raw).all() or code.min() < 0 or code.max() > 255
            or np.max(np.abs(raw - code)) > 2 ** -15):
        raise Failure('VALIDATION_FAILED', 'Image pixels do not match the declared byte data domain')
    return code.astype(np.uint8).reshape(height, width, 4)


def _source_image(spec):
    entry = spec['source_image']
    image = bpy.data.images.get(entry['name'])
    if (image is None or image.source != 'FILE' or len(image.packed_files) != 1
            or image.colorspace_settings.name != 'Non-Color' or image.alpha_mode != 'CHANNEL_PACKED'):
        raise Failure('UNSUPPORTED', 'Select an explicit packed Non-Color, CHANNEL_PACKED normal image')
    data = image.packed_file.data
    if hashlib.sha256(data).hexdigest() != entry['packed_sha256']:
        raise Failure('CONFLICT', 'Packed source image SHA mismatch')
    if (len(data) < 33 or data[:8] != PNG or data[12:16] != b'IHDR'
            or struct.unpack_from('>IIBBBBB', data, 16) != (*spec['source_size'], 8, 6, 0, 0, 0)):
        raise Failure('UNSUPPORTED', 'Grid transfer requires a noninterlaced packed 8-bit RGBA PNG')
    return _pixels(image, spec['source_size'])


def _masks(params, rows):
    archive = params['mask_archive']
    if archive is None:
        return None
    keys = {row['copy_mask'] + '.npy' for row in rows}
    try:
        with zipfile.ZipFile(archive['file']) as bundle:
            members = bundle.infolist()
            if len(members) != len(keys) or {entry.filename for entry in members} != keys:
                raise Failure('INVALID_REQUEST', 'Mask archive members must match the declared patch masks')
            if sum(entry.file_size for entry in members) > 4 * ATLAS_BUDGET:
                raise Failure('RESOURCE_LIMIT', 'Decoded patch masks exceed the grid memory budget')
            for entry in members:
                with bundle.open(entry) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        read_header = np.lib.format.read_array_header_1_0
                    elif version == (2, 0):
                        read_header = np.lib.format.read_array_header_2_0
                    else:
                        raise Failure('INVALID_REQUEST', 'Unsupported NPY mask header')
                    shape, _, dtype = read_header(stream)
                    if (len(shape) != 2 or min(shape) <= 0 or dtype != np.dtype(bool)
                            or stream.tell() + shape[0] * shape[1] != entry.file_size):
                        raise Failure('INVALID_REQUEST', 'Invalid boolean mask dimensions or byte count')
        with np.load(archive['file'], allow_pickle=False) as data:
            return {row['patch']: (row['copy_origin'], data[row['copy_mask']]) for row in rows}
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise Failure('INVALID_REQUEST', 'Cannot read the declared NPZ copy masks') from exc


def _encode_png(path, pixels):
    height, width, _ = pixels.shape
    rows = np.zeros((height, width * 4 + 1), dtype=np.uint8)
    rows[:, 1:] = pixels[::-1].reshape(height, width * 4)
    with path.open('xb') as out:
        out.write(PNG)
        for tag, value in ((b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)),
                           (b'IDAT', zlib.compress(rows.tobytes(), 6)), (b'IEND', b'')):
            out.write(struct.pack('>I', len(value)))
            out.write(tag)
            out.write(value)
            out.write(struct.pack('>I', zlib.crc32(value, zlib.crc32(tag))))


def _verify_png(path, pixels, size):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        image.alpha_mode = 'CHANNEL_PACKED'
        if not np.array_equal(_pixels(image, size), pixels):
            raise Failure('VALIDATION_FAILED', 'Written PNG changed at least one stored RGBA byte')
    finally:
        bpy.data.images.remove(image)


def run(params, job):
    params = normalize(params)
    job = Path(job).resolve()
    final = job / 'grid-transfer'
    report = {'operation': 'grid-transfer', 'status': 'running', 'candidate': None,
              'implementation_sha256': params['implementation_sha256'],
              'inputs': [{k: params[k] for k in ('file', 'expected_sha256')}, *params['resources']],
              'runtime': _runtime_descriptor(), 'sample_domain': params['manifest']['sample_domain'],
              'source_image': params['manifest']['source_image'],
              'tangent_tolerance_degrees': TANGENT_TOLERANCE_DEGREES}
    progress = _progress(report, job, time.monotonic())
    spec = params['manifest']
    try:
        if final.exists():
            raise Failure('CONFLICT', 'Grid transfer output already exists')
        with ExitStack() as guards:
            source_guard = _guard_inputs(params, guards)
            progress('grid_read_saved_inputs')
            _open_source(params)
            source_pixels = _source_image(spec)
            src, dst, patches, report['mesh_pairs'] = _read_geometry(params)
            rows = spec['placements']
            progress('grid_verify_footprint')
            plan = grid_transfer.plan_transfer(src, dst, patches,
                                                {row['patch']: row['translation'] for row in rows},
                                                spec['source_size'], spec['destination_size'],
                                                subtexel_divisions=spec['subtexel_divisions'],
                                                copy_masks=_masks(params, rows), test_budget=spec['test_budget'])
            report['footprint'] = plan.report
            for basis in report['mesh_pairs']:
                worst = basis['max_tangent_angle_degrees']
                if (not basis['source_destination_normal_and_handedness_exact']
                        or worst is None or worst > TANGENT_TOLERANCE_DEGREES):
                    raise Failure('VALIDATION_FAILED', 'Tangent basis changed or is degenerate: '
                                  + basis['source_object'])
            width, height = spec['destination_size']
            fill = np.empty((height, width, 4), dtype=np.uint8)
            fill[...] = spec['source_image']['fill_rgba8']
            pixels, receipt = grid_transfer.apply_transfer(plan, source_pixels, destination_pixels=fill)
            report['footprint'] = receipt
            progress('grid_write_staged_png')
            with tempfile.TemporaryDirectory(prefix='grid-transfer-', dir=job) as temporary:
                staged = Path(temporary) / final.name
                staged.mkdir()
                path = staged / 'normal.png'
                _encode_png(path, pixels)
                _verify_png(path, pixels, spec['destination_size'])
                saved = descriptor(path)
                saved['file'] = str(final / path.name)
                saved.update(size=spec['destination_size'], semantic=spec['source_image']['semantic'],
                             colorspace='Non-Color', alpha_mode='CHANNEL_PACKED',
                             byte_decode_exact=True, fill_rgba8=spec['source_image']['fill_rgba8'])
                if source_guard.sha256() != params['expected_sha256']:
                    raise Failure('CONFLICT', 'Source changed before grid PNG publication')
                progress('grid_publish')
                if final.exists():
                    raise Failure('CONFLICT', 'Grid transfer output already exists')
                report.update(status='texture_written', stage='complete', candidate=saved,
                              user_review='requires_saved_material_and_target_renderer_verification')
                atomic_json(staged / 'receipt.json', report)
                staged.rename(final)
    except grid_transfer.Refusal as exc:
        report.update(status='refused', candidate=None,
                      footprint=grid_transfer.refusal_report(exc))
        atomic_json(job / 'uv-report.json', report)
        raise Failure('VALIDATION_FAILED', 'Grid footprint refused: ' + str(exc.detail)) from exc
    except Failure as exc:
        report.update(status='failed', candidate=None, error={'code': exc.code, 'message': str(exc)})
        atomic_json(job / 'uv-report.json', report)
        raise
    atomic_json(job / 'uv-report.json', report)
    return report
