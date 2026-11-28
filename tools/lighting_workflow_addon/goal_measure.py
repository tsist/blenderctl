"""Raw scene-linear measurement for one camera, frame and view layer."""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np

from . import core


def _set_temporarily(stack, owner, **fields):
    previous = [(key, getattr(owner, key)) for key in fields]

    def restore():
        for key, value in previous:
            setattr(owner, key, value)

    stack.callback(restore)
    for key, value in fields.items():
        setattr(owner, key, value)


def _luminance_coefficients(header):
    chromaticities = header.get('chromaticities')
    if chromaticities is None:
        if path := os.environ.get('OCIO'):
            try:
                import PyOpenColorIO as ocio
                config = ocio.Config.CreateFromFile(path)
                role = config.getColorSpace(config.getRoleColorSpace('scene_linear'))
            except (ImportError, RuntimeError, ValueError) as exc:
                raise core.WorkflowError('UNSUPPORTED', 'Cannot identify the configured scene-linear color space') from exc
            if role is None or role.getInteropID() != 'lin_rec709_scene':
                core._fail('UNSUPPORTED', 'EXR must declare primaries for a non-Rec.709 scene-linear role')
        return np.array([.2126, .7152, .0722])
    xy = np.asarray(chromaticities, dtype=float).reshape((4, 2))
    if not np.isfinite(xy).all() or (xy[:, 1] == 0).any():
        core._fail('VALIDATION_FAILED', 'EXR has invalid color primaries')
    xyz = np.column_stack((xy[:, 0] / xy[:, 1], np.ones(4), (1 - xy.sum(axis=1)) / xy[:, 1]))
    try:
        return np.linalg.solve(xyz[:3].T, xyz[3])
    except np.linalg.LinAlgError:
        core._fail('VALIDATION_FAILED', 'EXR color primaries are degenerate')


def _read_metrics(exr, layer_name, regions, indices, groups=()):
    channels = {name: channel for part in exr.parts for name, channel in part.channels.items()}
    try:
        object_index = channels[layer_name + '.Object Index.X'].pixels if indices else None
    except KeyError:
        core._fail('VALIDATION_FAILED', 'EXR lacks Object Index: ' + str(sorted(channels)))
    coefficients = _luminance_coefficients(exr.header())

    def report(pass_name):
        prefix = layer_name + '.' + pass_name + '.'
        try:
            rgb = np.stack([channels[prefix + key].pixels for key in 'RGB'], axis=-1)
        except KeyError:
            core._fail('VALIDATION_FAILED', 'EXR lacks pass ' + pass_name + ' in ' + str(sorted(channels)))
        return _region_luminance(rgb @ coefficients, object_index, regions, indices)

    combined = report('Combined')
    if groups:
        return combined, [report('Combined_' + group) for group in groups]
    return combined


def _region_luminance(luminance, object_index, regions, indices):
    height, width = luminance.shape
    out = []
    for region in regions:
        if 'object' in region:
            selection = luminance[object_index == indices[region['object']]]
        else:
            left, top, right, bottom = region['roi']
            x0, x1 = np.searchsorted((np.arange(width) + .5) / width, [left, right])
            y0, y1 = np.searchsorted((np.arange(height) + .5) / height, [top, bottom])
            selection = luminance[y0:y1, x0:x1]
        if selection.size == 0:
            core._fail('UNMEASURABLE', 'Region has no visible sampled pixels: ' + str(region))
        value = float(selection.mean(dtype=float))
        if not np.isfinite(value):
            core._fail('UNMEASURABLE', 'Region has invalid scene-linear luminance: ' + str(region))
        out.append(value)
    return out


def _device(stack, scene, spec):
    bpy = core._bpy()
    if spec['backend'] == 'CPU':
        _set_temporarily(stack, scene.cycles, device='CPU')
        return {'backend': 'CPU'}
    preferences = bpy.context.preferences.addons['cycles'].preferences
    _set_temporarily(stack, preferences, compute_device_type=spec['backend'])
    devices = preferences.get_devices_for_type(spec['backend'])
    selected = [d for d in devices if d.type == spec['backend'] and d.id == spec['id']]
    if len(selected) != 1:
        core._fail('NOT_FOUND', 'Exact requested Cycles device was not detected: ' + spec['id'])
    for device in preferences.devices:
        _set_temporarily(stack, device, use=device == selected[0])
    _set_temporarily(stack, scene.cycles, device='GPU')
    return {'backend': spec['backend'], 'id': selected[0].id, 'name': selected[0].name}


@contextmanager
def measurement(scene, layer, regions, sampling, lights=()):
    """Yield a reusable measurement callback. Render settings and masks are temporary."""
    try:
        import OpenEXR
    except ImportError as exc:
        raise core.WorkflowError('UNSUPPORTED', 'Blender OpenEXR Python module is required') from exc
    bpy = core._bpy()
    if scene.camera is None or scene.camera.name not in scene.objects:
        core._fail('NOT_FOUND', 'Active scene camera is required')
    object_names = sorted({region['object'] for region in regions if 'object' in region})
    used_indices = {item.pass_index for item in bpy.data.objects}
    free_indices = (value for value in range(1, 32768) if value not in used_indices)
    indices = {}
    with ExitStack() as stack:
        directory = stack.enter_context(TemporaryDirectory(prefix='lw-goal-'))
        file = str(Path(directory) / 'measurement.exr')
        for name in object_names:
            obj = layer.objects.get(name)
            if not obj or obj.hide_render:
                core._fail('UNMEASURABLE', 'Object is outside the render view layer: ' + name)
            if obj.library or obj.override_library:
                core._fail('UNSUPPORTED', 'Linked object cannot receive a temporary IndexOB ID: ' + name)
            index = next(free_indices, None)
            if index is None:
                core._fail('UNMEASURABLE', 'No unused object index values')
            _set_temporarily(stack, obj, pass_index=index)
            indices[name] = index
        render = scene.render
        width = sampling['width']
        height = max(4, round(width * render.resolution_y / render.resolution_x))
        if height > 4096:
            core._fail('INVALID_REQUEST', 'Sampling height is larger than 4096')
        _set_temporarily(stack, render, engine='CYCLES', resolution_x=width, resolution_y=height,
                         resolution_percentage=100, use_border=False, use_multiview=False,
                         use_compositing=False, use_sequencer=False, use_motion_blur=False,
                         use_persistent_data=False, threads_mode='FIXED', threads=sampling['threads'], filepath=file)
        _set_temporarily(stack, scene.cycles, samples=sampling['samples'],
                         use_adaptive_sampling=False, use_denoising=False, seed=0, use_animated_seed=False)
        _set_temporarily(stack, layer, use_pass_combined=True, use_pass_object_index=bool(indices), samples=0)
        for item in scene.view_layers:
            _set_temporarily(stack, item, use=(item == layer))
        _set_temporarily(stack, render.image_settings, media_type='MULTI_LAYER_IMAGE',
                         file_format='OPEN_EXR_MULTILAYER', color_depth='32', color_mode='RGBA',
                         color_management='FOLLOW_SCENE', exr_codec='ZIP')
        selected_device = _device(stack, scene, sampling.get('device', {'backend': 'CPU'}))
        groups = []
        if lights:
            namespace = 'LW_' + uuid4().hex[:12] + '_'
            for i, light in enumerate(lights):
                name = namespace + str(i)
                group = layer.lightgroups.add(name=name)
                stack.callback(layer.lightgroups.remove, group)
                _set_temporarily(stack, light, lightgroup=name)
                groups.append(name)

        def sample():
            layer.update()
            result = bpy.ops.render.render(write_still=True, scene=scene.name, layer=layer.name)
            if 'FINISHED' not in result:
                core._fail('RENDER_FAILED', 'Lighting measurement render did not finish')
            with OpenEXR.File(file, separate_channels=True) as exr:
                values = _read_metrics(exr, layer.name, regions, indices, groups)
            return values

        if lights:
            yield sample, [width, height], selected_device
        else:
            yield sample, [width, height]
