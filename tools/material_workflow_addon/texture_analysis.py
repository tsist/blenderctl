# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, read-only texture diagnostics. No aesthetic or physical acceptance."""
import hashlib
import math
import os
from array import array

MAX_IMAGES = 64
MAX_PIXELS = 16777216
MAX_FILE_BYTES = 268435456
MAX_SAMPLES = 65536


def _linear(v):
    return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4


def _correlation(a, b):
    n = len(a)
    am, bm = sum(a) / n, sum(b) / n
    aa = sum((x-am)**2 for x in a)
    bb = sum((x-bm)**2 for x in b)
    if aa <= 1e-12 or bb <= 1e-12:
        return None
    return sum((x-am)*(y-bm) for x, y in zip(a, b)) / math.sqrt(aa*bb)


def analyze_manifest(batch_manifest, resource_paths=None, sample_limit=16384):
    """Analyze normalized batch assignments using private Blender FILE probes.

    resource_paths maps requested source paths to verified transport locations.
    Non-Color private probes expose stored channel values; sRGB color roles are
    explicitly linearized for luminance. Existing datablocks are never reused.
    Statistics describe deterministic samples, NOT all pixels or alignment.
    """
    import bpy
    report = {'status': 'pass', 'warnings': [], 'images': [], 'comparisons': [],
              'limits': {'max_images': MAX_IMAGES, 'max_samples_per_image': MAX_SAMPLES,
                         'max_decoded_pixels': MAX_PIXELS, 'max_file_bytes': MAX_FILE_BYTES,
                         'max_float_staging_bytes': MAX_PIXELS*16},
              'limitations': ['pass means sampled numerical checks only, not aesthetic acceptance or physical calibration',
                              'Deterministic samples can miss sparse defects; fractions are sampled estimates',
                              'Equal size/UV and correlation do not prove master alignment or pigment contamination',
                              'Blender decodes a complete image; one bounded float staging buffer is used; private probes are sequential and released',
                              'No vector normalization, channel repair, image edits, node edits or UV edits are performed']}
    def warn(code, severity='review', **context):
        report['warnings'].append(dict(code=code, severity=severity, **context))
        if severity == 'error': report['status'] = 'failed'
        elif report['status'] != 'failed': report['status'] = 'review_required'
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int) or not 1 <= sample_limit <= MAX_SAMPLES:
        warn('invalid_sample_limit', 'error', allowed=[1, MAX_SAMPLES])
        return report
    resources = resource_paths or {}
    cache, refs = {}, []
    for assignment in batch_manifest.get('assignments', []):
        for layer in assignment.get('layers', []):
            channels = dict(layer.get('channels', {}))
            if layer.get('mask'): channels['mask'] = layer['mask']
            for role, spec in channels.items():
                refs.append((assignment, layer, role, spec))
    if len(refs) > 512:
        warn('too_many_channel_references', 'error', count=len(refs), limit=512)
        return report
    if len({(r[3]['file'], r[2], r[3]['color_space'], r[3]['expected_sha256']) for r in refs}) > MAX_IMAGES:
        warn('too_many_images', 'error', limit=MAX_IMAGES)
        return report
    layer_refs = {}
    for assignment, layer, role, spec in refs:
        key = (spec['file'], role, spec['color_space'], spec['expected_sha256'])
        use = {'assignment': assignment.get('id'), 'layer': layer['id'], 'role': role,
               'uv_layer': assignment['target']['uv_layer'], 'mapping': layer.get('mapping', {}),
               'enabled': layer.get('enabled', True)}
        if key in cache:
            entry, samples = cache[key]
            entry['uses'].append(use)
        else:
            path = os.path.abspath(bpy.path.abspath(resources.get(spec['file'], spec['file'])))
            entry = {'source': spec['file'], 'resolved_file': path, 'role': role,
                     'declared_color_space': spec['color_space'], 'expected_sha256': spec['expected_sha256'],
                     'uses': [use], 'status': 'pass', 'sampling': 'uniform pixel-centre stratified linear indices'}
            report['images'].append(entry)
            samples, probe, pixels, rgb = None, None, None, None
            try:
                size = os.path.getsize(path)
                entry['bytes'] = size
                if size > MAX_FILE_BYTES: raise ValueError('file_byte_limit')
                with open(path, 'rb') as handle: digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                entry['sha256'] = digest
                if digest.lower() != spec['expected_sha256'].lower(): raise ValueError('sha256_mismatch')
                if role not in ('base_color', 'emission') and spec['color_space'] != 'Non-Color':
                    raise ValueError('data_role_requires_non_color')
                probe = bpy.data.images.load(path, check_existing=False)
                w, h = probe.size
                entry['size'] = [w, h]
                if probe.source != 'FILE' or w <= 0 or h <= 0: raise ValueError('image_decode_failed')
                if w*h > MAX_PIXELS: raise ValueError('decoded_pixel_limit')
                # Never sample through an sRGB transform for normal/data roles.
                probe.colorspace_settings.name = 'Non-Color'
                probe.reload()
                if len(probe.pixels) != w*h*4: raise ValueError('unsupported_pixel_layout')
                # RNA per-index reads repeatedly convert the image buffer. One bounded
                # bulk staging buffer is substantially faster than sparse RNA access.
                pixels = array('f', [0.0]) * (w*h*4)
                probe.pixels.foreach_get(pixels)
                count = min(sample_limit, w*h)
                indices = [min(w*h-1, ((2*i+1)*w*h)//(2*count)) for i in range(count)]
                rgb = [tuple(float(pixels[4*j+c]) for c in range(3)) for j in indices]
                if any(not math.isfinite(v) for p in rgb for v in p): raise ValueError('nonfinite_pixels')
                if any(v < 0 or v > 1 for p in rgb for v in p):
                    warn('out_of_unit_range', file=path, role=role); entry['status'] = 'review_required'
                entry['sample_count'] = count
                entry['sample_channel_ranges'] = [[min(p[c] for p in rgb), max(p[c] for p in rgb)] for c in range(3)]
                entry['pixel_semantics'] = 'stored channel data via private Non-Color probe'
                if role == 'normal':
                    vectors = [tuple(2*v-1 for v in p) for p in rgb]
                    lengths = [math.sqrt(sum(v*v for v in p)) for p in vectors]
                    bias = [sum(p[c] for p in vectors)/count for c in range(2)]
                    entry['normal'] = {'convention': 'OpenGL +Y tangent assumed by workflow; orientation not proven',
                        'length_min': min(lengths), 'length_max': max(lengths), 'length_mean': sum(lengths)/count,
                        'length_mean_abs_error': sum(abs(v-1) for v in lengths)/count,
                        'z_nonpositive_fraction_sampled': sum(p[2] <= 0 for p in vectors)/count,
                        'xy_bias_sampled': bias, 'derivative_calibration': 'not_proven', 'alignment': 'not_proven'}
                    risks = []
                    if any(p[2] <= 0 for p in vectors): risks.append('normal_nonpositive_z')
                    if sum(abs(v-1) for v in lengths)/count > .05: risks.append('normal_length_deviation')
                    if max(abs(v) for v in bias) > .03: risks.append('normal_xy_bias')
                    for risk in risks: warn(risk, file=path, role=role,
                        recommendation='Inspect stored normal and white-base normal-off/on renders; do not auto repair or infer calibration')
                    if risks: entry['status'] = 'review_required'
                samples = [.2126*p[0]+.7152*p[1]+.0722*p[2] for p in
                           ([tuple(_linear(v) for v in p) for p in rgb] if role in ('base_color','emission') and spec['color_space']=='sRGB' else rgb)]
                entry['comparison_semantics'] = 'linear luminance' if role in ('base_color','emission') else 'raw data luminance proxy (shader scalar conversion may differ)'
                with open(path, 'rb') as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
                        raise ValueError('source_changed_during_analysis')
            except Exception as error:
                entry['status'] = 'failed'
                entry['error'] = str(error)
                warn('texture_analysis_failed', 'error', file=path, role=role, reason=str(error))
            finally:
                if probe is not None: bpy.data.images.remove(probe)
                pixels, rgb = None, None
            cache[key] = (entry, samples)
        layer_refs.setdefault((assignment.get('id'), layer['id']), {})[role] = (entry, samples, use)
    for (aid, lid), roles in layer_refs.items():
        if 'base_color' not in roles or 'roughness' not in roles: continue
        color, cs, cu = roles['base_color']; rough, rs, ru = roles['roughness']
        comparison = {'assignment': aid, 'layer': lid, 'alignment': 'not_proven',
                      'interpretation': 'High absolute correlation is a review hint, never automatic rejection'}
        if cs is not None and rs is not None and color.get('size') == rough.get('size') and cu['uv_layer'] == ru['uv_layer'] and cu['mapping'] == ru['mapping']:
            corr = _correlation(cs, rs)
            comparison.update(status='sampled', sample_count=len(cs), pearson_linear_luminance_vs_data=corr)
            if corr is not None and abs(corr) >= .65:
                warn('pigment_roughness_correlation_review', assignment=aid, layer=lid, correlation=corr,
                     recommendation='Review whether pigment should share continuous glaze; do not auto reject')
        else: comparison.update(status='not_comparable', reason='Decode, size, UV or mapping differs')
        report['comparisons'].append(comparison)
    return report
