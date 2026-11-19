"""Host-safe request contract for the protected UV grid texture writer."""
import os

from protocol import Failure


COMMAND = 'uv.grid-transfer'
TEXEL_DOMAIN = 'BASE_LEVEL_LINEAR'
SEMANTIC = 'TANGENT_NORMAL_RGBA8'
ATLAS_BUDGET = 16_777_216
TEST_BUDGET = 150_000_000


def _require(condition, message, code='INVALID_REQUEST'):
    if not condition:
        raise Failure(code, message)


def _shape(value, fields, optional=(), label='record'):
    _require(isinstance(value, dict) and set(fields) <= set(value)
             and set(value) <= set(fields) | set(optional),
             label + ' has unexpected or missing fields')


def _int(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _pair(value, lower, upper):
    return isinstance(value, list) and len(value) == 2 and all(
        _int(v, lower, upper) for v in value)


def _name(value):
    return isinstance(value, str) and 0 < len(value) <= 255 and '\0' not in value


def normalize(value):
    from uv_workflow_contract import _descriptor, _identity, _sha

    params, identity = _identity(value)
    _shape(params, ('file', 'expected_sha256', 'destination', 'manifest'),
           ('mask_archive', 'resources'), 'UV grid params')
    source = _descriptor({k: params[k] for k in ('file', 'expected_sha256')},
                         'source', blend=True)
    destination = _descriptor(params['destination'], 'destination', blend=True)
    _require(os.path.normcase(source['file']) != os.path.normcase(destination['file']),
             'Source and destination must be distinct saved BLEND files')
    spec = params['manifest']
    _shape(spec, ('version', 'source_scene', 'source_size', 'destination_size',
                  'source_image', 'sample_domain', 'objects', 'placements', 'subtexel_divisions'),
           ('test_budget',), 'UV grid manifest')
    _require(spec['version'] == '1.0' and spec['sample_domain'] == TEXEL_DOMAIN,
             'Grid transfer requires version 1.0 and BASE_LEVEL_LINEAR sampling')
    _require(_name(spec['source_scene']), 'source_scene must name a saved scene')
    for key in ('source_size', 'destination_size'):
        size = spec[key]
        _require(_pair(size, 1, ATLAS_BUDGET) and size[0] * size[1] <= ATLAS_BUDGET,
                 key + ' exceeds the supported positive atlas size', 'RESOURCE_LIMIT')
    divisions = spec['subtexel_divisions']
    _require(_int(divisions, 2, 1 << 24) and divisions & (divisions - 1) == 0,
             'subtexel_divisions must be a power of two >= 2')
    spec.setdefault('test_budget', TEST_BUDGET)
    _require(_int(spec['test_budget'], 1, TEST_BUDGET), 'Invalid grid test budget')
    image = spec['source_image']
    _shape(image, ('name', 'packed_sha256', 'semantic', 'fill_rgba8'), label='source_image')
    _require(_name(image['name']) and image['semantic'] == SEMANTIC,
             'Grid transfer currently expects the declared TANGENT_NORMAL_RGBA8 source')
    _sha(image['packed_sha256'], 'source_image.packed_sha256')
    fill = image['fill_rgba8']
    _require(isinstance(fill, list) and len(fill) == 4 and all(_int(v, 0, 255) for v in fill),
             'fill_rgba8 must contain four byte values in RGBA order')

    objects = spec['objects']
    _require(isinstance(objects, list) and 0 < len(objects) <= 32,
             'Grid transfer requires 1..32 explicit mesh pairs')
    identifiers = set()
    pairs = {'source_object': set(), 'destination_object': set()}
    total_faces = 0
    for row in objects:
        _shape(row, ('source_object', 'destination_object', 'source_uv',
                     'destination_uv', 'patch_ids'), label='grid mesh pair')
        for key in ('source_object', 'destination_object', 'source_uv', 'destination_uv'):
            _require(_name(row[key]), key + ' must be a nonempty data-block name')
        for key, seen in pairs.items():
            _require(row[key] not in seen, key + ' must be unique')
            seen.add(row[key])
        ids = row['patch_ids']
        _require(isinstance(ids, list) and ids and all(_int(v, 0, 1_000_000) for v in ids),
                 'patch_ids must contain a nonnegative integer for each saved face')
        total_faces += len(ids)
        _require(total_faces <= 500_000, 'Grid transfer exceeds 500,000 triangles', 'RESOURCE_LIMIT')
        identifiers.update(ids)

    mask_archive = params.get('mask_archive')
    if mask_archive is not None:
        mask_archive = _descriptor(mask_archive, 'mask_archive')
        _require(mask_archive['file'].lower().endswith('.npz'), 'mask_archive must be an NPZ')
    placements = spec['placements']
    _require(isinstance(placements, list) and 0 < len(placements) <= 4096,
             'Grid transfer requires 1..4096 explicit placements')
    placed, keys = set(), set()
    for row in placements:
        fields = ('patch', 'translation', 'copy_origin', 'copy_mask') if mask_archive else ('patch', 'translation')
        _shape(row, fields, label='grid placement')
        patch = row['patch']
        _require(_int(patch, 0, 1_000_000) and patch not in placed, 'Each placement patch must be unique')
        _require(_pair(row['translation'], -ATLAS_BUDGET, ATLAS_BUDGET),
                 'Grid translation must contain two integers in texels')
        placed.add(patch)
        if mask_archive:
            key = row['copy_mask']
            valid_key = isinstance(key, str) and 0 < len(key) <= 64 and key.isascii() and key.isidentifier()
            _require(_pair(row['copy_origin'], 0, ATLAS_BUDGET) and valid_key and key not in keys,
                     'Invalid or duplicate explicit mask record')
            keys.add(key)
    _require(placed == identifiers, 'Each assigned patch needs exactly one placement')
    resources = [destination] + ([mask_archive] if mask_archive else [])
    _require(params.get('resources', resources) == resources,
             'Grid resources are derived from the destination and optional mask archive')
    return {**source, 'destination': destination, 'mask_archive': mask_archive, 'manifest': spec,
            'resources': resources, 'implementation_sha256': identity}
