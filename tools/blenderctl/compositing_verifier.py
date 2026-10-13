"""Fresh-process compositor reader and independent PNG/EXR decoder; never saves."""
import json
import math
import os
from pathlib import Path
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import Failure, atomic_json, digest, read_json
from compositing_temporal import validate_fresh_verification_budget, TemporalProjectionError, project as project_temporal, inflate as inflate_temporal, fresh_verification_budget

_IMMUTABLE_IMAGE_DECODE_SCOPE = ContextVar('compositing_immutable_image_decode_scope', default=None)


@contextmanager
def immutable_image_decode_scope(provider):
    """Host-private, attempt-only file inventory reuse under real immutable guards."""
    from compositing_media import ImmutableMediaDigests
    if type(provider) is not ImmutableMediaDigests or not provider.active or _IMMUTABLE_IMAGE_DECODE_SCOPE.get() is not None:
        raise Failure('CONFLICT', 'Independent image decode scope requires one live immutable media provider')
    token = _IMMUTABLE_IMAGE_DECODE_SCOPE.set(provider)
    try:
        yield
    finally:
        _IMMUTABLE_IMAGE_DECODE_SCOPE.reset(token)


def near(left, right, path='root'):
    if isinstance(left, bool) or isinstance(right, bool):
        return None if type(left) is type(right) and left == right else path
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return None if math.isfinite(left) and math.isfinite(right) and math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-6) else path
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return path + '.keys'
        return next((result for key in left if (result := near(left[key], right[key], path + '.' + key))), None)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return path + '.length'
        return next((result for index, (a, b) in enumerate(zip(left, right)) if (result := near(a, b, path + '[' + str(index) + ']'))), None)
    return None if type(left) is type(right) and left == right else path


def runtime_identity():
    import bpy
    build = bpy.app.build_hash.decode()
    if tuple(bpy.app.version) != (5, 2, 1) or build != '9e2066aef7ef':
        raise Failure('UNSUPPORTED', 'Static compositing workflows require Blender 5.2.1 build 9e2066aef7ef')
    return {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version),
            'build': build, 'binary_sha256': digest(bpy.app.binary_path)}


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return list(value)


def _fields(owner, names):
    return {name: _plain(getattr(owner, name)) for name in names if hasattr(owner, name)}


RENDER_FIELDS = ('engine', 'filepath', 'resolution_x', 'resolution_y', 'resolution_percentage',
                 'pixel_aspect_x', 'pixel_aspect_y', 'use_border', 'border_min_x', 'border_max_x',
                 'border_min_y', 'border_max_y', 'use_crop_to_border', 'film_transparent',
                 'use_compositing', 'use_sequencer', 'use_multiview', 'use_motion_blur',
                 'motion_blur_shutter', 'motion_blur_position', 'use_persistent_data',
                 'compositor_device', 'compositor_precision', 'compositor_denoise_device',
                 'compositor_denoise_preview_quality', 'compositor_denoise_final_quality', 'use_render_cache',
                 'use_texture_cache', 'use_auto_generate_texture_cache', 'use_stamp',
                 'use_file_extension', 'dither_intensity')
FORMAT_FIELDS = ('media_type', 'file_format', 'color_mode', 'color_depth', 'exr_codec',
                 'quality', 'compression', 'color_management')
VIEW_FIELDS = ('view_transform', 'look', 'exposure', 'gamma', 'use_curve_mapping',
               'use_white_balance', 'white_balance_temperature', 'white_balance_tint')
CYCLES_FIELDS = ('device', 'samples', 'use_adaptive_sampling', 'use_denoising', 'denoiser',
                 'seed', 'use_animated_seed')
EEVEE_FIELDS = ('taa_render_samples',)


def scene_configuration(scene):
    """Only values the M1 preview can change, plus original color/time context."""
    return {'frame': scene.frame_current, 'subframe': scene.frame_subframe,
            'camera': scene.camera.name if scene.camera else None,
            'render': _fields(scene.render, RENDER_FIELDS),
            'image_format': _fields(scene.render.image_settings, FORMAT_FIELDS),
            'image_format_color': {
                'view': _fields(scene.render.image_settings.view_settings, VIEW_FIELDS),
                'display': _fields(scene.render.image_settings.display_settings, ('display_device', 'emulation')),
                'linear': _fields(scene.render.image_settings.linear_colorspace_settings, ('name',)),
                'has_linear_colorspace': scene.render.image_settings.has_linear_colorspace},
            'view': _fields(scene.view_settings, VIEW_FIELDS),
            'display': _fields(scene.display_settings, ('display_device', 'emulation')),
            'cycles': _fields(scene.cycles, CYCLES_FIELDS) if hasattr(scene, 'cycles') else {},
            'eevee': _fields(scene.eevee, EEVEE_FIELDS) if hasattr(scene, 'eevee') else {},
            'layers': {layer.name: layer.use for layer in scene.view_layers},
            'markers': {marker.name: marker.camera.name if marker.camera else None for marker in scene.timeline_markers}}


def color_identity(scene):
    """Observe RNA and the configured OCIO file; never infer scene-linear primaries."""
    import bpy
    result = {'view': _fields(scene.view_settings, (*VIEW_FIELDS, 'is_hdr', 'white_balance_whitepoint')),
              'display': _fields(scene.display_settings, ('display_device', 'emulation')),
              'sequencer_input_space': scene.sequencer_colorspace_settings.name,
              'working_space': {'status': 'not_observed', 'reason': 'No scene working-space RNA in this runtime'},
              'ocio': {'status': 'not_observed'},
              'scope': 'Actual Blender RNA plus identified configuration; sequencer input space is not the compositor working space'}
    mapping = scene.view_settings.curve_mapping
    result['view_curve_mapping'] = None if mapping is None else {
        'settings': _fields(mapping, ('use_clip', 'clip_min_x', 'clip_min_y', 'clip_max_x', 'clip_max_y',
                                     'black_level', 'white_level', 'tone', 'extend')),
        'curves': [{'points': [{'location': list(point.location), 'handle_type': point.handle_type}
                                                      for point in curve.points]} for curve in mapping.curves]}
    configured = os.environ.get('OCIO')
    if not configured:
        result['ocio']['reason'] = 'No explicit OCIO environment identity; bundled defaults are not assumed active'
        return result
    if not Path(configured).is_absolute() or not Path(configured).is_file():
        result['ocio']['reason'] = 'Configured OCIO path is not an existing absolute file'
        return result
    path = Path(configured).resolve()
    result['ocio'] = {'status': 'identified_configured_file', 'file': str(path), 'sha256': digest(path),
                      'discovery': 'Inherited OCIO environment path; Blender exposes no active-config pointer RNA',
                      'luts': [], 'unresolved_file_transforms': []}
    try:
        import PyOpenColorIO as ocio
        config = ocio.Config.CreateFromFile(str(path))
        role = config.getRoleColorSpace('scene_linear')
        space = config.getColorSpace(role) if role else None
        result['working_space'] = {'status': 'configured_scene_linear_role' if space else 'not_observed',
                                   'name': role, 'interop_id': space.getInteropID() if space else None,
                                   'evidence': 'PyOpenColorIO role from SHA-identified OCIO environment configuration'}
        result['ocio'].update(reader='PyOpenColorIO', reader_version=ocio.__version__,
                              cache_id=config.getCacheID(), search_paths=list(config.getSearchPaths()))
        serialized = config.serialize()
        transforms = re.findall(r'!<FileTransform>\s*\{([^}]+)\}', serialized)
        result['ocio']['file_transform_scan'] = 'complete' if len(transforms) == serialized.count('!<FileTransform>') else 'partial_not_observed'
        context = config.getCurrentContext()
        seen = set()
        for transform in transforms:
            source = re.search(r'\bsrc:\s*("[^"]*"|\x27[^\x27]*\x27|[^,}]+)', transform)
            if source is None:
                result['ocio']['unresolved_file_transforms'].append({'status': 'not_observed', 'reason': 'FileTransform source unavailable'})
                continue
            source = source.group(1).strip().strip('\x27"')
            try:
                resolved = Path(context.resolveFileLocation(source)).resolve()
                if str(resolved) not in seen:
                    result['ocio']['luts'].append({'file': str(resolved), 'sha256': digest(resolved), 'configured_source': source})
                    seen.add(str(resolved))
            except Exception as error:
                result['ocio']['unresolved_file_transforms'].append({'configured_source': source,
                    'status': 'not_observed', 'reason': str(error)})
    except Exception as error:
        result['ocio']['configuration_reader'] = {'status': 'not_observed', 'reason': str(error)}
    return result


def output_color_space(scene, color, format_name):
    """Actual output-format RNA interpretation; configured role and saved EXR space are distinct."""
    fmt = scene.render.image_settings
    result = {'color_management': fmt.color_management,
              'scope': 'PNG applies the requested scene view/display; EXR uses actual format linear-space RNA without display baking'}
    if format_name != 'OPEN_EXR':
        result.update(name=scene.display_settings.display_device, interop_id=None, status='display_device_RNA')
        return result
    result.update(name=fmt.linear_colorspace_settings.name, interop_id=None,
                  has_linear_colorspace=fmt.has_linear_colorspace, status='linear_space_RNA')
    if color['ocio'].get('file'):
        try:
            import PyOpenColorIO as ocio
            config = ocio.Config.CreateFromFile(color['ocio']['file'])
            space = config.getColorSpace(result['name'])
            result['interop_id'] = space.getInteropID() if space else None
        except Exception as error:
            result['configuration_interop'] = {'status': 'not_observed', 'reason': str(error)}
    return result


def color_file_descriptors(color):
    """Only SHA-identified configuration/LUT files, never inferred defaults."""
    ocio = (color or {}).get('ocio') or {}
    rows = [ocio, *ocio.get('luts', [])]
    found = {}
    for row in rows:
        if not row.get('file') or not row.get('sha256'):
            continue
        path = str(Path(row['file']).resolve())
        key = os.path.normcase(path)
        if key in found and found[key]['sha256'] != row['sha256']:
            raise Failure('CONFLICT', 'Color identity has conflicting file SHAs: ' + path)
        found[key] = {'file': path, 'sha256': row['sha256']}
    return list(found.values())


def image_file_snapshot(binding, required_outputs=(), *, inventory_only=False, immutable_digest=None):
    provider = _IMMUTABLE_IMAGE_DECODE_SCOPE.get()
    decode = lambda: _image_file_snapshot_uncached(binding, required_outputs,
        inventory_only=inventory_only, immutable_digest=provider if provider else immutable_digest)
    if provider is not None:
        return provider.decoded_image_snapshot(binding, required_outputs, inventory_only, decode)
    return decode()


def _image_file_snapshot_uncached(binding, required_outputs=(), *, inventory_only=False, immutable_digest=None):
    """Decode static flat/multilayer inputs independently of Blender Image.size."""
    import OpenImageIO as oiio
    import numpy as np
    path = Path(binding['file'])
    if not path.is_file():
        raise Failure('CONFLICT', 'Bound image input SHA differs: ' + str(path))
    actual_sha = immutable_digest(path) if immutable_digest else digest(path)
    if actual_sha != binding['expected_sha256']:
        raise Failure('CONFLICT', 'Bound image input SHA differs: ' + str(path))
    reader = oiio.ImageInput.open(str(path))
    if reader is None:
        raise Failure('VALIDATION_FAILED', 'Bound image cannot be independently decoded: ' + oiio.geterror())
    parts = []
    total_values = 0
    try:
        container = reader.format_name().lower()
        for index in range(129):
            if not reader.seek_subimage(index, 0):
                break
            if index == 128:
                raise Failure('RESOURCE_LIMIT', 'Static bound image exceeds 128 subimages')
            spec = reader.spec()
            if spec.deep or spec.width < 1 or spec.height < 1 or spec.depth != 1:
                raise Failure('UNSUPPORTED', 'Bound image must contain ordinary static 2D pixels')
            total_values += spec.width * spec.height * spec.nchannels
            if total_values > 128000000:
                raise Failure('RESOURCE_LIMIT', 'Bound image independent-decode value budget exceeded')
            pixels = reader.read_image(format=oiio.FLOAT)
            values = np.asarray(pixels) if pixels is not None else None
            if values is None or values.shape != (spec.height, spec.width, spec.nchannels) or not np.isfinite(values).all():
                raise Failure('VALIDATION_FAILED', 'Bound image contains non-finite or malformed pixels')
            channels = list(spec.channelnames)
            part = {'index': index, 'name': spec.get_string_attribute('oiio:subimagename'),
                    'channels': channels, 'width': spec.width, 'height': spec.height,
                    'precision': str(spec.format), 'finite': True,
                    'channel_precision': [str(value) for value in spec.channelformats],
                    'metadata_colorspace': spec.get_string_attribute('oiio:ColorSpace') or None,
                    'color_interop_id': spec.get_string_attribute('colorInteropID') or None}
            alpha_index = next((i for i, channel in enumerate(channels) if channel.rsplit('.', 1)[-1] == 'A'), None)
            if alpha_index is not None:
                alpha = values[:, :, alpha_index]
                part['alpha'] = {'channel': channels[alpha_index], 'min': float(alpha.min()),
                                 'max': float(alpha.max()), 'finite': True}
                if float(alpha.min()) < -1e-6 or float(alpha.max()) > 1 + 1e-6:
                    raise Failure('VALIDATION_FAILED', 'Bound image alpha is outside 0..1')
            parts.append(part)
        if not parts:
            raise Failure('VALIDATION_FAILED', 'Bound image contains no independently decoded subimages')
        selected = []
        layer = binding.get('layer')
        if inventory_only:
            selected = parts
        elif layer is not None:
            if container != 'openexr':
                raise Failure('INVALID_REQUEST', 'Explicit layer requires an actual OpenEXR container')
            selected = [part for part in parts if part['name'].startswith(layer + '.')]
            if not selected:
                # Single-part multichannel files may expose all passes in one part.
                selected = [part for part in parts if any(channel.startswith(layer + '.') for channel in part['channels'])]
            if not selected:
                raise Failure('NOT_FOUND', 'Independent EXR layer unavailable: ' + layer)
            for output in required_outputs:
                pass_name = 'Combined' if output == 'Alpha' else output
                prefix = layer + '.' + pass_name + '.'
                channels = [channel for part in selected for channel in part['channels'] if channel.startswith(prefix)]
                if not channels or (output == 'Alpha' and not any(channel.endswith('.A') for channel in channels)):
                    raise Failure('UNSUPPORTED', 'Independent EXR required Pass/channels unavailable: ' + layer + '/' + output)
                wanted = ({'R', 'G', 'B'} if output in ('Combined', 'Noisy Image', 'Denoising Albedo', 'Denoising Specular Albedo') else
                          {'X', 'Y', 'Z'} if output in ('Normal', 'Denoising Normal') else
                          {'Z'} if output in ('Depth', 'Denoising Depth') else {'A'} if output == 'Alpha' else set())
                if not wanted.issubset({channel.rsplit('.', 1)[-1] for channel in channels}):
                    raise Failure('UNSUPPORTED', 'Independent EXR required Pass components missing: ' + layer + '/' + output)
        else:
            if len(parts) != 1 or any(part['name'] for part in parts):
                raise Failure('INVALID_REQUEST', 'Multilayer input requires an explicit actual layer binding')
            selected = parts
            if 'Alpha' in required_outputs and 'alpha' not in parts[0]:
                raise Failure('UNSUPPORTED', 'Bound flat image has no required Alpha channel')
        return {'file': str(path.resolve()), 'sha256': binding['expected_sha256'], 'reader': 'OpenImageIO',
                'container': container, 'bytes': path.stat().st_size, 'subimages': parts,
                'selected_layer': layer, 'selected_subimages': [part['index'] for part in selected],
                'required_outputs': sorted(set(required_outputs)),
                'selection_scope': 'container inventory only; actual node layer not claimed' if inventory_only else 'explicit binding layer and required outputs',
                'interpretation': {'requested_colorspace': binding['colorspace'], 'requested_alpha_mode': binding['alpha_mode'],
                                   'scope': 'File metadata and declared Blender interpretation are separate; no implicit input resize or colorspace conversion here'}}
    finally:
        reader.close()


def render_pass_snapshot(scene, layer, required_outputs):
    """Observe actual switches independently; never enable missing passes."""
    settings = {prop.identifier: bool(getattr(layer, prop.identifier)) for prop in layer.bl_rna.properties
                if prop.type == 'BOOLEAN' and prop.identifier.startswith('use_pass_')}
    cycles = getattr(layer, 'cycles', None)
    if cycles:
        settings['cycles'] = {prop.identifier: bool(getattr(cycles, prop.identifier)) for prop in cycles.bl_rna.properties
                              if prop.type == 'BOOLEAN' and ('pass' in prop.identifier or 'denoising' in prop.identifier)}
    switches = {'Image': 'use_pass_combined', 'Alpha': 'use_pass_combined', 'Depth': 'use_pass_z',
                'Normal': 'use_pass_normal', 'Noisy Image': 'use_pass_combined'}
    denoising = {'Denoising Albedo', 'Denoising Specular Albedo', 'Denoising Normal', 'Denoising Roughness', 'Denoising Depth'}
    requirements = []
    for output in sorted(set(required_outputs)):
        if output in switches:
            switch = switches[output]
            enabled = settings.get(switch, False)
            if output == 'Noisy Image':
                enabled = enabled and scene.render.engine == 'CYCLES'
        elif output in denoising:
            switch = 'cycles.denoising_store_passes'
            enabled = scene.render.engine == 'CYCLES' and settings.get('cycles', {}).get('denoising_store_passes', False)
        else:
            raise Failure('UNSUPPORTED', 'Render Pass is outside finite static M2 scope: ' + output)
        if not enabled:
            raise Failure('UNSUPPORTED', 'Required render Pass switch is disabled: ' + output + '/' + switch)
        requirements.append({'output': output, 'switch': switch, 'enabled': True})
    return {'scene': scene.name, 'view_layer': layer.name, 'engine': scene.render.engine,
            'actual_switches': settings, 'requirements': requirements, 'status': 'passed'}


def binding_snapshot(inspection):
    """Explicit core input inventory, actual RNA references and independent file reader."""
    import bpy
    if inspection.get('workflow_version') not in ('1.1', '1.2', '1.3'):
        return {'status': 'not_applicable_no_m2_inventory', 'inputs': []}
    if inspection.get('status') != 'managed':
        return {'status': 'not_run_nonmanaged_inspection', 'inputs': []}
    if 'input_bindings' not in inspection:
        raise Failure('VALIDATION_FAILED', 'M2 core did not supply its explicit input binding inventory')
    rows = []
    for declared in inspection['input_bindings']:
        tree = bpy.data.node_groups.get(declared['tree'])
        node = tree.nodes.get(declared['node']) if tree else None
        if node is None or node.bl_idname != declared['type']:
            raise Failure('CONFLICT', 'Explicit bound input reference differs')
        spec = declared['binding']
        required = sorted(set(declared['required_outputs']))
        available = {socket.identifier: socket for socket in node.outputs}
        if any(identifier not in available or not available[identifier].enabled or
               getattr(available[identifier], 'is_unavailable', False) for identifier in required):
            raise Failure('UNSUPPORTED', 'Required actual bound output socket is unavailable')
        row = {key: declared[key] for key in ('node_id', 'node', 'tree', 'type', 'binding', 'observed')}
        row['required_outputs'] = required
        if spec['kind'] == 'image':
            image = node.image
            if not image or image.source != 'FILE' or image.type not in ('IMAGE', 'MULTILAYER'):
                raise Failure('UNSUPPORTED', 'Bound Image is not an ordinary static external image')
            actual = str(Path(bpy.path.abspath(image.filepath)).resolve())
            if actual != str(Path(spec['file']).resolve()) or image.colorspace_settings.name != spec['colorspace'] or image.alpha_mode != spec['alpha_mode']:
                raise Failure('CONFLICT', 'Bound Image actual file/interpretation differs')
            if image.type == 'MULTILAYER' and node.layer != spec.get('layer'):
                raise Failure('CONFLICT', 'Actual EXR layer differs from declared binding')
            if node.has_views:
                raise Failure('UNSUPPORTED', 'Multiview input is outside static M2')
            row['independent_file'] = image_file_snapshot(spec, required)
            row['actual_interpretation'] = {'image': image.name, 'type': image.type,
                'colorspace': image.colorspace_settings.name, 'alpha_mode': image.alpha_mode,
                'layer': node.layer if image.type == 'MULTILAYER' else None,
                'rna_size': list(image.size), 'size_scope': 'Multilayer RNA size may be zero; independent subimage dimensions are authoritative'}
        elif spec['kind'] in ('image_sequence', 'movie'):
            import compositing_media as media
            temporal = next((item for item in inspection.get('temporal_bindings', []) if item['node_id'] == declared['node_id']), None)
            if temporal is None:
                raise Failure('CONFLICT', 'Sequence lacks complete core temporal binding receipt')
            row['independent_movie' if spec['kind'] == 'movie' else 'independent_sequence'] = (media.movie_snapshot if spec['kind'] == 'movie' else media.sequence_snapshot)(spec, temporal['resolved_media'])
            row['actual_interpretation'] = {'image': node.image.name, 'type': node.image.type,
                'colorspace': node.image.colorspace_settings.name, 'alpha_mode': node.image.alpha_mode,
                'frame_start': node.frame_start, 'frame_duration': node.frame_duration,
                'frame_offset': node.frame_offset, 'use_cyclic': node.use_cyclic,
                'use_auto_refresh': node.use_auto_refresh}
        elif spec['kind'] == 'render_layers':
            scene = node.scene
            layer = scene.view_layers.get(node.layer) if scene else None
            if scene is None or layer is None or scene.name != spec['scene'] or layer.name != spec['view_layer']:
                raise Failure('CONFLICT', 'Actual Render Layers context differs')
            row['required_passes'] = render_pass_snapshot(scene, layer, required)
        else:
            raise Failure('UNSUPPORTED', 'Unknown M2 input binding kind')
        rows.append(row)
    return {'status': 'passed', 'inputs': rows,
            'baseline_definition': inspection.get('baseline_definition'),
            'scope': 'Static explicit bound inputs only; actual interpretation, required Pass switches and independent file subimages; no input resize'}


def resource_snapshot(resources, *, require_canonical=False):
    """Canonical ID paths and narrowly verified owned sequence receipts."""
    import bpy
    import compositing_media as media
    managed_images, managed_resources = media.persisted_media()
    declared = {os.path.normcase(str(Path(row['file']).resolve())): row['expected_sha256'] for row in resources}
    rows, retained = [], []
    collections = []
    aggregate = []
    for prop in bpy.data.bl_rna.properties:
        if prop.type == 'COLLECTION':
            if prop.identifier == 'all_ids':
                aggregate = getattr(bpy.data, prop.identifier)
            else:
                collections.append((prop.identifier, getattr(bpy.data, prop.identifier)))
    canonical = {}
    for kind, collection in collections:
        for item in collection:
            if not isinstance(item, bpy.types.ID) or not hasattr(item, 'filepath'):
                continue
            canonical.setdefault(item.as_pointer(), (kind, item))
    # all_ids is an aggregate alias, not a second dependency kind. Prove its
    # coverage instead of dropping unknown filepath-bearing IDs silently.
    for item in aggregate:
        if isinstance(item, bpy.types.ID) and hasattr(item, 'filepath') and item.as_pointer() not in canonical:
            raise Failure('UNSUPPORTED', 'Unclassified aggregate filepath ID: ' + item.bl_rna.identifier + '/' + item.name)
    for kind, item in canonical.values():
            raw = item.filepath
            if kind == 'cache_files':
                raise Failure('UNSUPPORTED', 'M1 has no cache-file binding contract: ' + item.name)
            if kind == 'images' and getattr(item, 'source', '') == 'SEQUENCE' and item.name in managed_images:
                binding = managed_images[item.name]
                is_movie = binding['declaration']['kind'] == 'movie'
                proof = (media.movie_snapshot if is_movie else media.sequence_snapshot)(binding['declaration'], binding['resolved_media'])
                retained.append({'kind': kind, 'name': item.name, 'reason': 'owned_decoded_movie_receipt' if is_movie else 'owned_sequence_receipt', 'proof': proof})
                continue
            if getattr(item, 'source', '') in ('SEQUENCE', 'MOVIE', 'TILED') or getattr(item, 'is_sequence', False) or any(token in raw for token in ('<UDIM>', '<udim>', '####')):
                raise Failure('UNSUPPORTED', 'M1 rejects temporal/tiled dependency: ' + kind + '/' + item.name)
            packed = bool(getattr(item, 'packed_file', None)) or bool(getattr(item, 'packed_files', []))
            if not raw or raw == '<builtin>' or packed:
                retained.append({'kind': kind, 'name': item.name, 'reason': 'packed' if packed else 'builtin_or_embedded'})
                continue
            library = getattr(item, 'library', None)
            resolved = str(Path(bpy.path.abspath(raw, library=library)).resolve())
            key = os.path.normcase(resolved)
            if key not in declared:
                raise Failure('CONFLICT', 'Undeclared compositor dependency: ' + resolved)
            if not Path(resolved).is_file() or digest(resolved) != declared[key]:
                raise Failure('CONFLICT', 'Compositor dependency SHA mismatch: ' + resolved)
            if require_canonical and not library and os.path.normcase(raw) != os.path.normcase(resolved):
                raise Failure('VALIDATION_FAILED', 'Saved local dependency is not canonical absolute: ' + kind + '/' + item.name)
            rows.append({'kind': kind, 'name': item.name, 'file': resolved, 'sha256': declared[key],
                         'stored_path': raw, 'linked': bool(library)})
    return {'status': 'checked_static_and_owned_sequence_paths' if managed_images else 'checked_static_id_paths',
            'external': rows, 'retained': retained, 'staged_resources': managed_resources,
            'aggregate_filepath_coverage': 'passed',
            'scope': 'Every canonical current ID filepath; all_ids aggregate coverage verified; packed/embedded retained; only owned verified sequence receipts accepted; unowned temporal/tiled/cache paths rejected'}


def decode_image(row):
    """OIIO reads actual container/precision, not the filename suffix."""
    import OpenImageIO as oiio
    import numpy as np
    path = Path(row['file'])
    if not path.is_file() or digest(path) != row['sha256']:
        raise Failure('CONFLICT', 'Rendered image SHA mismatch: ' + str(path))
    reader = oiio.ImageInput.open(str(path))
    if reader is None:
        raise Failure('VALIDATION_FAILED', 'Cannot independently decode output: ' + oiio.geterror())
    try:
        spec = reader.spec()
        actual_format = reader.format_name().lower()
        actual_precision = str(spec.format)
        if (spec.width, spec.height) != (row['width'], row['height']):
            raise Failure('VALIDATION_FAILED', 'Decoded dimensions differ: ' + str(path))
        wanted_format, wanted_precision = ('png', 'uint8') if row['format'] == 'PNG' else ('openexr', 'float')
        if actual_format != wanted_format or actual_precision != wanted_precision:
            raise Failure('VALIDATION_FAILED', 'Actual image container/precision differs: ' + str(path))
        channels = list(spec.channelnames)
        if channels != ['R', 'G', 'B', 'A']:
            raise Failure('VALIDATION_FAILED', 'Expected independent RGBA output: ' + str(channels))
        pixels = reader.read_image(format=oiio.FLOAT)
        if pixels is None:
            raise Failure('VALIDATION_FAILED', 'Decoded image has no pixels')
        values = np.asarray(pixels)
        if values.shape != (row['height'], row['width'], 4) or not np.isfinite(values).all():
            raise Failure('VALIDATION_FAILED', 'Non-finite or malformed output image')
        alpha = values[:, :, 3]
        if float(alpha.min()) < -1e-6 or float(alpha.max()) > 1 + 1e-6:
            raise Failure('VALIDATION_FAILED', 'Output Alpha outside 0..1')
        if reader.seek_subimage(1, 0):
            raise Failure('VALIDATION_FAILED', 'Static preview expected one image, not multi-layer output')
        metadata_space = spec.get_string_attribute('oiio:ColorSpace') or None
        interop = spec.get_string_attribute('colorInteropID') or None
        expected_interop = row.get('output_color_space', {}).get('interop_id')
        if actual_format == 'openexr' and expected_interop and interop and expected_interop != interop:
            raise Failure('VALIDATION_FAILED', 'Actual EXR colorInteropID differs from identified configured scene-linear role')
        return {**row, 'bytes': path.stat().st_size, 'decode': {'reader': 'OpenImageIO',
                'container': actual_format, 'precision': actual_precision, 'channels': channels,
                'width': spec.width, 'height': spec.height, 'finite': True,
                'min': float(values.min()), 'max': float(values.max()),
                'mean': [float(values[:, :, index].mean()) for index in range(4)],
                'alpha': {'min': float(alpha.min()), 'max': float(alpha.max()), 'finite': True},
                'color_metadata': {'colorspace': metadata_space, 'interop_id': interop,
                    'status': 'observed' if metadata_space or interop else 'not_observed',
                    'scope': 'Independent file metadata; PNG display values and EXR scene-linear role kept distinct'}}}
    finally:
        reader.close()


def verify(config_path):
    import bpy
    from compositing_identity import implementation_sha256
    from compositing_workflow_addon import core
    from filesystem import FileGuard
    from contextlib import ExitStack
    config = read_json(config_path)
    report = {'compositing_verification_version': '1.0', 'status': 'failed', 'outputs': []}
    try:
        if 'verification_budget' in config:
            try:
                actual_budget = validate_fresh_verification_budget(config['verification_budget'], len(config.get('temporal_evaluations', [])), len(config.get('outputs', [])))
            except TemporalProjectionError as exc:
                raise Failure('CONFLICT', 'Fresh reader declared work budget differs from complete verification request: ' + str(exc)) from exc
            report['verification_budget'] = actual_budget
        with ExitStack() as stack:
            report['runtime'] = runtime_identity()
            if report['runtime'] != config['runtime'] or implementation_sha256() != config['implementation_sha256']:
                raise Failure('CONFLICT', 'Runtime or compositor implementation differs in fresh reader')
            guarded = {}
            color_inputs = color_file_descriptors(config.get('color_identity'))
            for row in [config['candidate'], *config.get('resources', []), *config.get('staged_resources', []), *config.get('persisted_documents', []), *config.get('outputs', []), *color_inputs]:
                key = os.path.normcase(str(Path(row['file']).resolve()))
                expected = row.get('sha256', row.get('expected_sha256'))
                if key in guarded:
                    if guarded[key]['sha256'] != expected:
                        raise Failure('CONFLICT', 'Fresh reader duplicate input SHA differs: ' + row['file'])
                    continue
                guard = stack.enter_context(FileGuard(row['file']))
                if guard.sha256() != expected:
                    raise Failure('CONFLICT', 'Fresh reader input changed: ' + row['file'])
                guarded[key] = {'file': row['file'], 'sha256': expected, 'guard': guard}
            report['color_immutable_inputs'] = {'status': 'guarded' if color_inputs else 'not_observed',
                                                'files': color_inputs, 'count': len(color_inputs)}
            provider = None
            if config.get('inspection', {}).get('workflow_version') in ('1.2', '1.3'):
                import compositing_media as media
                provider = media.guarded_digest_scope(stack, guarded)
                report['immutable_media_digest_scope'] = provider.stats
            bpy.ops.wm.open_mainfile(filepath=config['candidate']['file'], load_ui=False, use_scripts=False)
            import compositing_media as media
            declared_fresh = {key: row['sha256'] for key, row in guarded.items()}
            media.persisted_documents(stack=stack, checked=guarded, allowed=declared_fresh)
            scene = bpy.data.scenes.get(config['scene'])
            if scene is None or scene.view_layers.get(config['view_layer']) is None:
                raise Failure('NOT_FOUND', 'Saved target Scene/ViewLayer missing')
            bpy.context.window.scene = scene
            bpy.context.window.view_layer = scene.view_layers[config['view_layer']]
            scene.frame_set(config['frame'])
            bpy.context.view_layer.update()
            report['resources'] = resource_snapshot(config.get('resources', []), require_canonical=True)
            report['input_file_preflight'] = [
                {'node_id': row['node_id'], 'independent_file': image_file_snapshot(row['binding'], row['required_outputs'])}
                for row in (config.get('input_bindings') or {}).get('inputs', []) if row['binding']['kind'] == 'image']
            inspection = json.loads(json.dumps(core.inspect(config['scene'], config['workflow_id'])))
            protection = json.loads(json.dumps(core.snapshot_protection(config['scene'], config['workflow_id'])))
            for label, wanted, actual in [('inspection', config['inspection'], inspection),
                                          ('non_target', config['protection'], protection),
                                          ('source_configuration', config['source_config'], scene_configuration(scene))]:
                mismatch = near(wanted, actual)
                if mismatch:
                    raise Failure('VALIDATION_FAILED', 'Fresh saved ' + label + ' differs at ' + mismatch)
            if config.get('color_identity') is not None:
                report['color_identity'] = color_identity(scene)
                mismatch = near(config['color_identity'], report['color_identity'])
                if mismatch:
                    raise Failure('VALIDATION_FAILED', 'Fresh saved color/OCIO identity differs at ' + mismatch)
            if config.get('input_bindings') is not None:
                report['input_bindings'] = binding_snapshot(inspection)
                mismatch = near(config['input_bindings'], report['input_bindings'])
                if mismatch:
                    raise Failure('VALIDATION_FAILED', 'Fresh bound input RNA/file/Pass evidence differs at ' + mismatch)
            report['outputs'] = [decode_image(row) for row in config.get('outputs', [])]
            report['temporal_evaluations'] = []
            report['temporal_definitions'] = {}
            for expected in reversed(config.get('temporal_evaluations', [])):
                actual = core.temporal_evaluation(config['scene'], config['workflow_id'], expected['scene_frame'])
                wanted = inflate_temporal(expected, config.get('temporal_definitions', {}))
                mismatch = near(wanted, actual)
                if mismatch:
                    raise Failure('VALIDATION_FAILED', 'Fresh out-of-order temporal evaluation differs: ' + mismatch)
                report['temporal_evaluations'].append(project_temporal(actual, report['temporal_definitions']))
            if digest(config['candidate']['file']) != config['candidate']['sha256']:
                raise Failure('CONFLICT', 'Fresh reader candidate changed during verification')
            for row in guarded.values():
                if row['guard'].sha256() != row['sha256']:
                    raise Failure('CONFLICT', 'Fresh reader guarded input changed during verification: ' + row['file'])
            report.update(status='passed', inspection=inspection, protection='passed',
                          source_configuration='passed', candidate_sha256=config['candidate']['sha256'],
                          limitations=['Read-only fresh Blender process; not GUI or visual quality acceptance'])
            if provider:
                provider.close()
    except Exception as exc:
        report['error'] = {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc),
                           'details': getattr(exc, 'details', None)}
    atomic_json(config['output'], report)
    return 0 if report['status'] == 'passed' else 8


if __name__ == '__main__':
    sys.exit(verify(sys.argv[sys.argv.index('--') + 1]))
