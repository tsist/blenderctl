"""Read or edit the native Toon manga look for a saved shot.

Use operation read-shot-look or edit-shot-look with edition, file, expected_sha256
and context {scene, view_layer, frame}. Edit accepts a sparse foreground or
background settings patch, region_mode (UNIFORM or SEPARATE), and an ordered
background_edits list. Each add names an object. Remove or replace names the
expected object and optionally its row index. Replace also names new_object.
Read returns stored settings even when the effect is off.
Use read-native-media or edit-native-media with the same saved-shot context for
native Beauty plus artistic lines. Supply enabled, line_mode (OVERLAY/EXCLUDE),
a sparse line_optics patch, or the ordered media_objects list of Mesh/Volume names.
"""
import math
import os
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, open_saved_context, require
from protocol import Failure, digest


MEDIA = {'read-native-media', 'edit-native-media'}
OPERATIONS = {'read-shot-look', 'edit-shot-look'} | MEDIA


def request(params, guards, products):
    common = {'operation', 'edition', 'file', 'expected_sha256', 'context'}
    edits = {'foreground', 'background', 'region_mode', 'background_edits'}
    require(isinstance(params, dict) and common <= params.keys() and
            isinstance(params['operation'], str) and params['operation'] in OPERATIONS,
            'INVALID_REQUEST', 'Expected read-shot-look or edit-shot-look and a saved scene')
    extra = params.keys() - common
    if params['operation'] == 'edit-native-media':
        require(extra and extra <= {'enabled', 'line_mode', 'line_optics', 'media_objects'},
                'INVALID_REQUEST', 'Supply only native media and artistic line settings')
    elif params['operation'] == 'edit-shot-look':
        require(extra and extra <= edits, 'INVALID_REQUEST', 'Specify the shot settings or background rows to edit')
    else:
        require(not extra, 'INVALID_REQUEST', 'Read only accepts source, edition and context')
    require(isinstance(params['edition'], str) and params['edition'] in products, 'UNSUPPORTED', 'Unknown Toon edition')
    ctx = params['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'} and
            all(isinstance(ctx[k], str) and ctx[k] for k in ('scene', 'view_layer')) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected a named scene, view layer and integer frame')
    require(isinstance(params['file'], str) and Path(params['file']).suffix.lower() == '.blend' and
            isinstance(params['expected_sha256'], str), 'INVALID_REQUEST', 'Supply a .blend and its SHA-256')
    params['file'] = str(guard_file(params['file'], guards, params['expected_sha256']))
    return params


def observation(ui, scene):
    settings = getattr(scene, ui.PROP)
    return {'foreground': ui._read_settings(settings, foreground=True),
            'background': ui._read_settings(settings.background), 'region_mode': settings.region_mode,
            'background_objects': [obj.name_full for obj in ui._background_objects(scene, settings)]}


def patch_settings(ui, group, patch, foreground):
    fields = set(ui.manga_look.DEFAULT_SETTINGS)
    require(isinstance(patch, dict) and patch and patch.keys() <= fields | ({'line_ink_strength'} if foreground else set()),
            'INVALID_REQUEST', 'Unknown or empty shot look settings patch')
    current = ui._read_settings(group, foreground=foreground)
    merged = {**current, **patch}
    strength = merged.pop('line_ink_strength', None)
    if foreground:
        require(type(strength) in (int, float) and math.isfinite(strength) and 0 <= strength <= 1,
                'INVALID_REQUEST', 'line_ink_strength must be between 0 and 1')
    try:
        normalized = dict(ui.manga_look.normalize_settings(merged))
    except (ValueError, TypeError) as error:
        raise Failure('INVALID_REQUEST', 'Invalid manga settings: ' + str(error)) from error
    if foreground:
        normalized['line_ink_strength'] = strength
    for key in patch:
        setattr(group, key, normalized[key])
    require(ui._close(ui._read_settings(group, foreground=foreground), normalized),
            'VALIDATION_FAILED', 'Saved RNA rejected or clamped a requested setting')


def background_edits(ui, scene, rows, steps):
    require(type(steps) is list and 0 < len(steps) <= 128, 'INVALID_REQUEST', 'Expected 1–128 background edits')
    for step in steps:
        require(isinstance(step, dict) and isinstance(step.get('object'), str),
                'INVALID_REQUEST', 'Each edit requires a named scene mesh')
        action = step.get('action')
        required = {'action', 'object', 'new_object'} if action == 'replace' else {'action', 'object'}
        optional = {'index'} if action in ('replace', 'remove') else set()
        require(action in ('add', 'replace', 'remove') and required <= step.keys() <= required | optional,
                'INVALID_REQUEST', 'Unexpected background edit fields or action')
        obj = scene.objects.get(step['object'])
        require(obj is not None and obj.type == 'MESH' and not obj.library, 'CONFLICT', 'Expected a local scene mesh')
        index = next((i for i, row in enumerate(rows) if row.object == obj), None)
        if action == 'add':
            require(index is None, 'CONFLICT', 'Mesh is already a background member')
            rows.add().object = obj
        else:
            require(index is not None, 'CONFLICT', 'Named mesh is no longer a background member')
            if 'index' in step:
                require(type(step['index']) is int and step['index'] == index, 'CONFLICT', 'Background row identity differs')
            if action == 'remove':
                rows.remove(index)
            else:
                require(isinstance(step['new_object'], str), 'INVALID_REQUEST', 'Expected the replacement mesh name')
                other = scene.objects.get(step['new_object'])
                require(other is not None and not other.library and other.type == 'MESH' and
                        all(row.object != other for i, row in enumerate(rows) if i != index),
                        'CONFLICT', 'Replacement must be a distinct local background mesh')
                rows[index].object = other
    ui._object_list(scene, [row.object for row in rows])


def media_observation(ui, scene):
    settings = getattr(scene, ui.PROP)
    optics = settings.line_optics
    return {'enabled': settings.enabled, 'line_mode': settings.line_mode,
            'line_optics': {k: list(optics.velocity_px) if k == 'velocity_px' else getattr(optics,k)
                            for k in ui.line_optics.DEFAULTS},
            'media_objects': [row.object.name_full if row.object else None for row in settings.media_objects]}


def edit_media(ui, scene, params):
    settings = getattr(scene, ui.PROP)
    if 'enabled' in params:
        require(type(params['enabled']) is bool, 'INVALID_REQUEST', 'enabled must be boolean')
        settings.enabled = params['enabled']
    if 'line_mode' in params:
        require(params['line_mode'] in ('OVERLAY', 'EXCLUDE'), 'INVALID_REQUEST', 'Unknown line participation mode')
        settings.line_mode = params['line_mode']
    if 'line_optics' in params:
        patch = params['line_optics']
        require(isinstance(patch, dict) and patch, 'INVALID_REQUEST', 'Specify the optical controls to change')
        try:
            normalized = ui.line_optics.normalize({**media_observation(ui, scene)['line_optics'], **patch})
        except (TypeError, ValueError) as exc:
            raise Failure('INVALID_REQUEST', str(exc)) from exc
        for key in patch:
            setattr(settings.line_optics, key, normalized[key])
        observed = media_observation(ui, scene)['line_optics']
        for key, value in normalized.items():
            actual = observed[key]
            if isinstance(value, list):
                matches = len(value) == len(actual) and all(math.isclose(x, y, rel_tol=1e-6) for x,y in zip(actual,value))
            elif isinstance(value, float):
                matches = math.isclose(actual,value,rel_tol=1e-6)
            else:
                matches = actual == value
            require(matches, 'INVALID_REQUEST', 'Blender could not retain optical control '+key)
    if 'media_objects' in params:
        names = params['media_objects']
        require(isinstance(names,list) and len(names) <= 256 and all(isinstance(n,str) and n for n in names)
                and len(set(names)) == len(names), 'INVALID_REQUEST', 'Supply up to 256 unique media object names')
        objects = [scene.objects.get(n) for n in names]
        require(all(o and o.type in ('MESH','VOLUME') and not o.library and not o.override_library for o in objects),
                'CONFLICT','Expected a local scene Mesh or Volume for each media member')
        settings.media_objects.clear()
        for obj in objects:
            settings.media_objects.add().object = obj


def edit_look(ui, scene, params):
    settings = getattr(scene, ui.PROP)
    if 'region_mode' in params:
        require(params['region_mode'] in ('UNIFORM', 'SEPARATE'), 'INVALID_REQUEST', 'Unknown region mode')
        settings.region_mode = params['region_mode']
    for key, group in (('foreground', settings), ('background', settings.background)):
        if key in params:
            patch_settings(ui, group, params[key], key == 'foreground')
    if 'background_edits' in params:
        background_edits(ui, scene, settings.background_objects, params['background_edits'])


def work(bpy, ui, params):
    from toon_panel_handoff import bindings, dependencies, workspace_brush_paths

    context = open_saved_context(bpy, params)
    ui.register()
    scene = context.scene
    media = params['operation'] in MEDIA
    observe = media_observation if media else observation
    active = 'active_media' if media else 'active_look'
    current = observe(ui, scene)
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}

    def protected():
        paths = dependencies(bpy)
        brushes = set(workspace_brush_paths(bpy, paths))
        return fingerprint({'bindings': bindings(bpy), 'dependencies': [p for p in paths if p not in brushes],
                            'other_scenes': {s.name_full: observe(ui, s) for s in bpy.data.scenes if s != scene}})

    signature = protected()
    result = {'operation': params['operation'], 'runtime': runtime, 'stored': current, active: ui.snapshot(scene)}
    if 'verify' in params:
        require(params['verify'] == {'stored': current, 'protected': signature}, 'VALIDATION_FAILED',
                'Cold shot look, mesh membership or other scene data differs')
    if params['operation'] in ('read-shot-look','read-native-media'):
        return result
    require(not scene.library and not scene.override_library, 'CONFLICT', 'Shot look editing requires a local scene')
    if media:
        edit_media(ui, scene, params)
    else:
        edit_look(ui, scene, params)
    after = observe(ui, scene)
    require(protected() == signature, 'VALIDATION_FAILED', 'Background edit changed other scene data')
    result.update(stored=after, verify={'stored': after, 'protected': signature})
    result[active] = ui.snapshot(scene)
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=params['stage'], copy=True, relative_remap=True,
                                       check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save the edited shot')
    result['output_sha256'] = digest(params['stage'])
    return result
