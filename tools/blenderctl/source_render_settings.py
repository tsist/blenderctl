"""SOURCE_V1 source-faithful render setting capture and preservation checks.

``capture_source`` is deliberately bpy-free: callers pass a live Blender
Scene/ViewLayer from the worker or the source probe.  The returned document is
JSON-safe and can be copied into a render.run manifest.

The first SOURCE_V1 version preserves the saved engine, sampling (including
Cycles adaptive sampling and seed state), denoiser/CPU-GPU selection, motion
blur shutter/position, persistent data and color management.  Output resolution
and PNG 8/16 depth are request-owned.  The render guard rejects multiview,
non-square pixels, border/crop, an enabled compositor graph, an enabled VSE
timeline with strips, additional active view layers and camera-marker switches
instead of silently changing the saved result.
"""
from protocol import Failure

SOURCE_ENGINES=('CYCLES','BLENDER_EEVEE','BLENDER_WORKBENCH')


def _layer_names(scene):
    return [str(layer.name) for layer in scene.view_layers]


def _active_layers(scene):
    return [str(layer.name) for layer in scene.view_layers if bool(getattr(layer,'use',False))]


def _camera_name(camera):
    return str(camera.name) if camera is not None else None


def _compositor_group(scene):
    group=getattr(scene,'compositing_node_group',None)
    return group if group is not None else getattr(scene,'node_tree',None)


def _scene_strips(scene):
    editor=getattr(scene,'sequence_editor',None)
    if editor is None:return ()
    strips=getattr(editor,'strips',None)
    return strips if strips is not None else getattr(editor,'sequences',())


def _samples(scene,engine):
    if engine=='CYCLES':return int(scene.cycles.samples)
    if engine in ('BLENDER_EEVEE','BLENDER_EEVEE_NEXT'):return int(scene.eevee.taa_render_samples)
    if engine=='BLENDER_WORKBENCH':
        raw=str(getattr(scene.display,'render_aa','8'))
        try:return int(raw)
        except (TypeError,ValueError):
            raise Failure('UNSUPPORTED','SOURCE_V1 Workbench source requires numeric eight-sample render_aa; got '+raw)
    raise Failure('UNSUPPORTED','Unsupported source render engine: '+str(engine))


def _markers(scene):
    return [{'name':str(getattr(marker,'name','')),'frame':int(marker.frame),
             'camera':_camera_name(getattr(marker,'camera',None))}
            for marker in scene.timeline_markers]


def capture_source(scene,layer):
    """Capture the saved settings that a SOURCE_V1 request must preserve."""
    if layer is None or str(layer.name) not in _layer_names(scene):
        raise Failure('INVALID_REQUEST','View layer does not belong to the source scene')
    render=scene.render
    engine=str(render.engine)
    samples=_samples(scene,engine)
    transparent=bool(render.film_transparent)
    view=str(scene.view_settings.view_transform)
    look=str(scene.view_settings.look)
    exposure=float(scene.view_settings.exposure)
    gamma=float(scene.view_settings.gamma)
    display_device=str(scene.display_settings.display_device)
    dither=float(render.dither_intensity)
    use_curve_mapping=bool(scene.view_settings.use_curve_mapping)
    use_motion_blur=bool(getattr(render,'use_motion_blur',False))
    shutter=float(getattr(render,'motion_blur_shutter',0.0))
    motion_blur=shutter if use_motion_blur else 0.0
    blur_position=str(getattr(render,'motion_blur_position','CENTER'))
    denoise=False;denoiser=None;denoising_use_gpu=None
    adaptive=False;seeded=False;seed=None;animated_seed=False
    adaptive_threshold=None;adaptive_min_samples=None;saved_device=None
    if engine=='CYCLES':
        cycles=scene.cycles
        denoise=bool(cycles.use_denoising)
        denoiser=str(cycles.denoiser) if denoise else None
        if denoise and hasattr(cycles,'denoising_use_gpu'):
            denoising_use_gpu=bool(cycles.denoising_use_gpu)
        adaptive=bool(getattr(cycles,'use_adaptive_sampling',False))
        animated_seed=bool(getattr(cycles,'use_animated_seed',False))
        seed=int(getattr(cycles,'seed',0))
        seeded=not animated_seed
        if hasattr(cycles,'adaptive_threshold'):adaptive_threshold=float(cycles.adaptive_threshold)
        if hasattr(cycles,'adaptive_min_samples'):adaptive_min_samples=int(cycles.adaptive_min_samples)
        saved_device=str(cycles.device)
    group=_compositor_group(scene)
    has_compositor_graph=bool(group is not None and len(getattr(group,'nodes',())))
    strips=_scene_strips(scene)
    effective={
        'engine':engine,
        'samples':samples,
        'adaptive':adaptive,
        'adaptive_threshold':adaptive_threshold,
        'adaptive_min_samples':adaptive_min_samples,
        'seeded':seeded,
        'seed':seed,
        'use_animated_seed':animated_seed,
        'device':saved_device,
        'saved_device':saved_device,
        'denoiser':denoiser,
        'denoising_use_gpu':denoising_use_gpu,
        'persistent_data':bool(getattr(render,'use_persistent_data',False)),
        'transparent':transparent,
        'film_transparent':transparent,
        'motion_blur':{'enabled':use_motion_blur,'shutter':shutter,'position':blur_position},
        'color':{'view':view,'look':look,'exposure':exposure,'gamma':gamma,
                 'display_device':display_device,'dither_intensity':dither,
                 'use_curve_mapping':use_curve_mapping},
        'display_device':display_device,
        'view_transform':view,
        'look':look,
        'exposure':exposure,
        'gamma':gamma,
        'dither_intensity':dither,
        'use_curve_mapping':use_curve_mapping,
        'resolution':{'width':int(render.resolution_x),'height':int(render.resolution_y),
                      'percentage':int(render.resolution_percentage),
                      'pixel_aspect_x':float(render.pixel_aspect_x),
                      'pixel_aspect_y':float(render.pixel_aspect_y)},
        'compositor':{'enabled':bool(getattr(render,'use_compositing',False)),
                      'has_graph':has_compositor_graph,
                      'group':str(group.name) if group is not None else None},
        'sequencer':{'enabled':bool(getattr(render,'use_sequencer',False)),
                     'strips':len(strips)},
        'multiview':bool(getattr(render,'use_multiview',False)),
        'border':{'use_border':bool(getattr(render,'use_border',False)),
                  'crop_to_border':bool(getattr(render,'use_crop_to_border',False))},
        'active_view_layers':_active_layers(scene),
        'camera':_camera_name(getattr(scene,'camera',None)),
        'camera_markers':_markers(scene),
        'frame_range':[int(scene.frame_start),int(scene.frame_end)],
        'fps':int(render.fps),
        'fps_base':float(render.fps_base),
        'scene':str(scene.name),
        'view_layer':str(layer.name),
    }
    result={'engine':engine,'samples':samples,'transparent':transparent,
            'color':{'view':view,'look':look,'exposure':exposure,'gamma':gamma},
            'denoise':denoise,'motion_blur':motion_blur,'effective':effective}
    if denoise:
        # These names let callers construct the matching render.run manifest.
        result['denoiser']=denoiser
        if hasattr(scene.cycles,'denoising_use_gpu'):
            result['denoising_use_gpu']=bool(scene.cycles.denoising_use_gpu)
    return result


def assert_source_preservable(scene,layer,camera,frames,*,allow_compositor=False):
    """Reject source features whose real effect cannot be preserved yet."""
    if layer is None or str(layer.name) not in _layer_names(scene):
        raise Failure('INVALID_REQUEST','View layer does not belong to the source scene')
    if camera is None:
        raise Failure('INVALID_REQUEST','A source camera is required')
    render=scene.render
    engine=str(render.engine)
    if engine not in SOURCE_ENGINES:
        raise Failure('UNSUPPORTED','SOURCE_V1 does not cover source engine: '+engine)
    if bool(getattr(render,'use_multiview',False)):
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve multiview/stereo rendering')
    pixel_aspect_x=float(render.pixel_aspect_x);pixel_aspect_y=float(render.pixel_aspect_y)
    if abs(pixel_aspect_x-pixel_aspect_y)>1e-6:
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve non-square pixels: '+str(pixel_aspect_x)+'x'+str(pixel_aspect_y))
    if bool(getattr(render,'use_border',False)) or bool(getattr(render,'use_crop_to_border',False)):
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve border/crop rendering')
    group=_compositor_group(scene)
    if not allow_compositor and bool(getattr(render,'use_compositing',False)) and group is not None and len(getattr(group,'nodes',())):
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve an enabled compositor graph; use explicit render.run or disable compositing')
    strips=_scene_strips(scene)
    if bool(getattr(render,'use_sequencer',False)) and len(strips):
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve an enabled VSE timeline with strips')
    other=[name for name in _active_layers(scene) if name!=str(layer.name)]
    if other:
        raise Failure('UNSUPPORTED','SOURCE_V1 cannot preserve additional active view layers: '+', '.join(sorted(other)))
    camera_name = _camera_name(camera)
    frames = tuple(int(frame) for frame in frames)
    start, end = min(frames), max(frames)
    cameras_at_frame = {}
    enabled_frames = set()
    for marker in scene.timeline_markers:
        marker_camera = getattr(marker, 'camera', None)
        if marker_camera is None:
            continue
        frame = int(marker.frame)
        cameras_at_frame.setdefault(frame, set()).add(_camera_name(marker_camera))
        if not bool(getattr(marker_camera, 'hide_render', False)):
            enabled_frames.add(frame)

    # Blender uses the last camera marker at/before the first frame, or the
    # first future camera marker if there is no earlier one. A sparse request
    # keeps the same cut boundaries as a contiguous request over its span.
    relevant = {frame for frame in cameras_at_frame if start < frame <= end}
    # A render-hidden camera may be skipped by Blender. Check the effective
    # start with and without hidden assignments, and keep their cut protection
    # inside the span. Render-time checks still verify every sampled frame.
    for marker_frames in (cameras_at_frame, enabled_frames):
        if marker_frames:
            first_future = min(marker_frames)
            relevant.add(max((frame for frame in marker_frames if frame <= start), default=first_future))

    for frame in sorted(relevant):
        names = cameras_at_frame[frame]
        if len(names) > 1:
            raise Failure('UNSUPPORTED', f'SOURCE_V1 has conflicting camera markers at frame {frame}: '
                          + ', '.join(sorted(names)))
        if camera_name not in names:
            raise Failure('UNSUPPORTED', f'SOURCE_V1 cannot preserve camera marker at frame {frame} '
                          f'for requested span {start}–{end}: {next(iter(names))} != {camera_name}')


def _same_float(left,right):
    return abs(float(left)-float(right))<=1e-6


def _mismatch(field,expected,actual):
    raise Failure('CONFLICT','SOURCE_V1 spec '+field+' differs from source: '+repr(expected)+' != '+repr(actual))


def verify_source_spec(spec,captured):
    """Verify the necessary source-named fields of a normalized render.run spec."""
    for field in ('engine','samples','transparent','denoise'):
        if spec.get(field)!=captured.get(field):_mismatch(field,captured.get(field),spec.get(field))
    if not _same_float(spec['motion_blur'],captured['motion_blur']):
        _mismatch('motion_blur',captured['motion_blur'],spec['motion_blur'])
    source_color=captured['color'];spec_color=spec['color']
    for field in ('view','look'):
        if field in spec_color and spec_color[field]!=source_color[field]:
            _mismatch('color.'+field,source_color[field],spec_color[field])
    for field in ('exposure','gamma'):
        if not _same_float(spec_color[field],source_color[field]):
            _mismatch('color.'+field,source_color[field],spec_color[field])
    if captured['denoise']:
        if 'denoiser' not in spec:
            raise Failure('INVALID_REQUEST','SOURCE_V1 spec must declare the source denoiser')
        if spec['denoiser']!=captured['denoiser']:
            _mismatch('denoiser',captured['denoiser'],spec['denoiser'])
        if 'denoising_use_gpu' in captured:
            if 'denoising_use_gpu' not in spec:
                raise Failure('INVALID_REQUEST','SOURCE_V1 spec must declare the source denoising_use_gpu')
            if bool(spec['denoising_use_gpu'])!=bool(captured['denoising_use_gpu']):
                _mismatch('denoising_use_gpu',captured['denoising_use_gpu'],spec['denoising_use_gpu'])
        elif 'denoising_use_gpu' in spec:
            raise Failure('UNSUPPORTED','Source runtime cannot verify denoising_use_gpu')


def preserved_source_signature(captured):
    """Return the source-owned subset that must stay constant across frames."""
    effective=captured['effective']
    return {
        'engine':captured['engine'],
        'samples':captured['samples'],
        'transparent':captured['transparent'],
        'color':captured['color'],
        'denoise':captured['denoise'],
        'motion_blur':captured['motion_blur'],
        'denoiser':captured.get('denoiser'),
        'denoising_use_gpu':captured.get('denoising_use_gpu'),
        'adaptive':effective.get('adaptive'),
        'adaptive_threshold':effective.get('adaptive_threshold'),
        'adaptive_min_samples':effective.get('adaptive_min_samples'),
        'seeded':effective.get('seeded'),
        'seed':None if effective.get('use_animated_seed') else effective.get('seed'),
        'use_animated_seed':effective.get('use_animated_seed'),
        'persistent_data':effective.get('persistent_data'),
        'motion_blur_effective':effective.get('motion_blur'),
        'display_device':effective.get('display_device'),
        'dither_intensity':effective.get('dither_intensity'),
        'use_curve_mapping':effective.get('use_curve_mapping'),
        'fps':effective.get('fps'),
        'fps_base':effective.get('fps_base'),
    }
