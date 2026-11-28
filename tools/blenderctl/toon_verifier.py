"""Cold process verifier; consumes the original sealed expectation."""
import json
import math
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import re
import sys
import traceback

PROJECT = next(p for p in Path(__file__).resolve().parents if (p / 'blenderctl.ps1').is_file())
sys.path.insert(0, str(PROJECT / 'tools'))
sys.path.insert(0, str(PROJECT / 'tools/blenderctl'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import atomic_json, digest, read_json
from toon_identity import implementation_sha256
from toon_workflow_addon import contract, state_contract

INPUT_KEYS = ('candidate', 'state', 'plan', 'expectation')
CONTENT_FIELDS = frozenset({'status', 'candidate', 'implementation_sha256', 'execution_context',
    'thread_observation', 'source_protection', 'visibility', 'image_checks', 'render_settings',
    'geometry', 'materials', 'chain_provenance', 'chain_provenance_validation', 'state_schema',
    'source_text_executed', 'limits', 'reopen_schema', 'bound_inputs', 'warm_images'})


def _descriptor(row, extra=()):
    if (type(row) is not dict or set(row) != {'file', 'sha256', 'bytes', *extra}
            or not isinstance(row['file'], str) or not Path(row['file']).is_absolute()
            or not isinstance(row['sha256'], str) or re.fullmatch('[0-9a-f]{64}', row['sha256']) is None
            or type(row['bytes']) is not int or row['bytes'] < 0):
        raise RuntimeError('Invalid pinned verification descriptor')


def _cpu_context(value):
    if (type(value) is not dict or set(value) != {'cpu_threads'}
            or type(value['cpu_threads']) is not int or value['cpu_threads'] <= 0):
        raise RuntimeError('Verifier requires the original explicit CPU execution context')


def _validate_placement_validation(value, manifest, frame, visible_objects):
    fields = {'status', 'groups', 'screen_tolerance_px', 'depth_relative_tolerance'}
    if (type(value) is not dict or set(value) != fields or value['status'] != 'passed'
            or type(value['screen_tolerance_px']) is not float or value['screen_tolerance_px'] != .0005
            or type(value['depth_relative_tolerance']) is not float or value['depth_relative_tolerance'] != 2e-6
            or type(value['groups']) is not list):
        raise RuntimeError('Placement validation contract is incomplete')
    state_contract.validate_frame_report(frame, frame['frame'], manifest)
    expected = frame['groups']
    groups = value['groups']
    if len(groups) != len(expected):
        raise RuntimeError('Placement validation group domain differs')
    group_fields = {'group_id', 'objects', 'placement', 'vertex_count', 'triangle_count',
                    'max_screen_error_px', 'max_depth_error', 'expected_depth_max', 'depth_tolerance_max'}
    for group, prior in zip(groups, expected):
        if (type(group) is not dict or set(group) != group_fields
                or any(group[key] != prior[key] for key in ('group_id', 'objects', 'placement'))
                or any(type(group[key]) is not int or group[key] < 0 for key in ('vertex_count', 'triangle_count'))
                or any(type(group[key]) not in (int, float) or not math.isfinite(group[key]) or group[key] < 0
                       for key in ('max_screen_error_px', 'max_depth_error', 'expected_depth_max', 'depth_tolerance_max'))):
            raise RuntimeError('Placement validation group proof differs from the bound frame')
        if (group['max_screen_error_px'] > .0005
                or group['depth_tolerance_max'] != 2e-6 * max(1, group['expected_depth_max'])
                or group['max_depth_error'] > group['depth_tolerance_max']):
            raise RuntimeError('Placement validation measurement exceeds its fixed bound')
        if not group['objects']:
            if any(group[key] != 0 for key in ('vertex_count', 'triangle_count', 'max_screen_error_px', 'max_depth_error', 'expected_depth_max')):
                raise RuntimeError('Empty placement group has nonempty measurements')
        elif (group['vertex_count'] == 0 or group['triangle_count'] == 0 or group['expected_depth_max'] <= 0
                or (group['placement']['mode'] == 'camera_plane'
                    and group['expected_depth_max'] != group['placement']['camera_depth'])):
            raise RuntimeError('Nonempty placement group lacks its expected depth/counts')
    if (sorted(name for group in groups for name in group['objects']) != visible_objects
            or sum(group['vertex_count'] for group in groups) != frame['counts']['vertices']
            or sum(group['triangle_count'] for group in groups) != frame['counts']['faces']):
        raise RuntimeError('Placement proof object or geometry inventory differs from the bound frame')


def validate_content_receipt(content, request, manifest=None, embedded_frames=None, embedded_animation=None, embedded_materials=None):
    """Pure structure/binding validation; importing this module needs no bpy."""
    if (type(request) is not dict or not {*INPUT_KEYS, 'implementation_sha256', 'execution_context',
            'images', 'chain_provenance'} <= set(request)):
        raise RuntimeError('Content receipt request bindings are incomplete')
    placed = contract.is_placed(manifest)
    animated = contract.is_animated(manifest)
    fields = CONTENT_FIELDS | ({'shading_animation'} if animated else set())
    if (type(content) is not dict or set(content) != fields
            or content['status'] != 'content_passed' or content['reopen_schema'] != ('toon-reopen-content-v2' if placed else 'toon-reopen-content-v1')
            or content['image_checks'] != []):
        raise RuntimeError('A complete content-only receipt is required')
    _cpu_context(request.get('execution_context'))
    _cpu_context(content['execution_context'])
    for key in INPUT_KEYS:
        _descriptor(request[key])
    if type(content['bound_inputs']) is not dict or set(content['bound_inputs']) != set(INPUT_KEYS):
        raise RuntimeError('Content receipt bound inputs are incomplete')
    for key in INPUT_KEYS:
        _descriptor(content['bound_inputs'][key])
    _descriptor(content['candidate'])
    if (content['bound_inputs'] != {key: request[key] for key in INPUT_KEYS}
            or content['candidate'] != request['candidate']
            or content['implementation_sha256'] != request['implementation_sha256']
            or content['execution_context'] != request['execution_context']
            or content['warm_images'] != request['images']
            or content['chain_provenance'] != request.get('chain_provenance')):
        raise RuntimeError('Content receipt inputs/implementation/context/warm images/provenance differ')
    if (content['state_schema'] != (state_contract.state_schema(manifest) if manifest else state_contract.STATE_SCHEMA)
            or content['chain_provenance_validation'] != 'full_canonical_fingerprint'
            or content['source_text_executed'] is not False
            or content['geometry'] != 'passed' or content['materials'] != 'passed'
            or type(content['source_protection']) is not dict
            or content['source_protection'].get('status') != 'passed'
            or content['source_protection'].get('differences') != []
            or type(content['source_protection'].get('checked')) is not int
            or content['source_protection']['checked'] < 0
            or type(content['render_settings']) is not dict
            or content['render_settings'].get('status') != 'passed'):
        raise RuntimeError('Content receipt protection/settings checks are incomplete')
    if manifest and manifest['schema_version'] in contract.REGION_VERSIONS:
        proof = content['source_protection'].get('material_regions')
        expected = {'status': 'passed', 'schema': 'toon-region-materials-v1',
                    'target_count': len(manifest['targets']), 'assignment_count': len(manifest['shading']['assignments']),
                    'source_mapping': 'exact_preflight_slots_links_and_polygon_indices'}
        if proof != expected:
            raise RuntimeError('Content regional source mapping proof differs')
    if animated:
        from toon_workflow_addon import animation_state
        if content['shading_animation'] != embedded_animation or type(embedded_frames) is not list:
            raise RuntimeError('Content Action/reference witness differs from the original embedded state')
        if embedded_materials is None:raise RuntimeError('Animated receipt requires bound managed materials')
        animation_state.validate_state(embedded_animation, manifest, embedded_animation['reference']['frame'], embedded_frames, embedded_materials)
    actual = content['render_settings'].get('actual', {})
    domain = actual.get('frames') if type(actual) is dict else None
    if (type(domain) is not list or len(domain) != 2 or any(type(v) is not int for v in domain)
            or domain[0] > domain[1] or domain[1] - domain[0] + 1 > 72):
        raise RuntimeError('Content receipt frame domain is invalid')
    start, end = domain
    frames = list(range(start, end + 1))
    order = list(dict.fromkeys([end, start, (start + end) // 2, *range(end, start - 1, -1)]))
    visibility = content['visibility']
    if (type(visibility) is not list
            or any(type(row) is not dict or type(row.get('frame')) is not int
                   or row.get('status') != 'passed' or type(row.get('visible_objects')) is not list
                   or any(not isinstance(name, str) or not name for name in row['visible_objects'])
                   or len(set(row['visible_objects'])) != len(row['visible_objects']) for row in visibility)
            or [row['frame'] for row in visibility] != order):
        raise RuntimeError('Content receipt full visibility replay is incomplete')
    if placed:
        if (type(embedded_frames) is not list or len(embedded_frames) != len(frames)
                or any(type(row) is not dict or row.get('frame') != frame for row, frame in zip(embedded_frames, frames))):
            raise RuntimeError('Placement validation requires the complete bound state frame domain')
        for row in visibility:
            if set(row) != {'frame', 'visible_objects', 'status', 'placement_validation'} | ({'shading_sample'} if animated else set()):
                raise RuntimeError('Placement visibility row has unexpected or missing proof fields')
            _validate_placement_validation(row['placement_validation'], manifest,
                                           embedded_frames[row['frame'] - start], row['visible_objects'])
            if animated:
                if row['shading_sample'] != embedded_frames[row['frame']-start]['shading_sample']:
                    raise RuntimeError('Cold actual shading sample differs from the bound producer frame')
    for provenance in (content['chain_provenance'], request.get('chain_provenance')):
        if (type(provenance) is not list or any(type(row) is not dict or type(row.get('frame')) is not int for row in provenance)
                or [row['frame'] for row in provenance] != frames):
            raise RuntimeError('Content receipt provenance frame domain differs')
        for row in provenance:
            _descriptor(row, ('frame', 'schema'))
            if row['schema'] != state_contract.chain_schema(manifest):
                raise RuntimeError('Content receipt provenance schema differs')
    for images in (content['warm_images'], request['images']):
        if (type(images) is not list or any(type(row) is not dict or type(row.get('frame')) is not int for row in images)
                or [row['frame'] for row in images] not in ([], frames)):
            raise RuntimeError('Content receipt warm image frame domain is partial')
        for row in images:
            if not {'file', 'sha256', 'bytes'} <= set(row):
                raise RuntimeError('Content receipt warm image descriptor is incomplete')
            _descriptor({key: row[key] for key in ('file', 'sha256', 'bytes')})
            if animated and row.get('shading_sample') != embedded_frames[row['frame']-start]['shading_sample']:
                raise RuntimeError('Warm image lacks its bound actual shading sample')
    if (type(content['thread_observation']) is not dict or type(content['limits']) is not list
            or any(not isinstance(item, str) for item in content['limits'])
            or type(content['thread_observation'].get('cpu_threads')) is not int
            or content['thread_observation']['cpu_threads'] != content['execution_context']['cpu_threads']):
        raise RuntimeError('Content receipt diagnostic fields are invalid')


def checked(row):
    path = Path(row['file'])
    if not path.is_file() or digest(path) != row['sha256'] or path.stat().st_size != row['bytes']:
        raise RuntimeError('Pinned verification input changed: ' + str(path))
    return path


def thread_observation():
    """Raw diagnostic RNA and argv; no Scene settings are changed."""
    import bpy
    from toon_workflow import execution_context
    observed = {'argv': list(sys.argv)}
    try:
        observed.update(execution_context())
    except Exception as exc:
        observed['context_error'] = str(exc)
    try:
        observed['scenes'] = [
            {'name': scene.name, 'library': scene.library.filepath if scene.library else None,
             'threads_mode': scene.render.threads_mode, 'threads': scene.render.threads}
            for scene in sorted(bpy.data.scenes, key=lambda item: (item.library.filepath if item.library else '', item.name))]
    except Exception as exc:
        observed['scene_observation_error'] = str(exc)
    return observed


def _content(request, actual_context, render_images=True):
    import bpy
    from toon_workflow_addon import core, materials, regions
    candidate = checked(request['candidate'])
    state = read_json(checked(request['state']))
    plan = read_json(checked(request['plan']))
    expectation = read_json(checked(request['expectation']))
    bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
    scene = bpy.data.scenes[plan['manifest']['scene']]; core.activate(scene)
    scene.frame_set(plan['reference_frame']); bpy.context.view_layer.update()
    collection = bpy.data.collections[plan['collection_name']]
    render_settings = core.validate_render_contract(scene, plan['manifest'])
    embedded = state['embedded_state']
    if state.get('schema') != state_contract.state_schema(plan['manifest']):
        raise RuntimeError('Cold state schema is unsupported')
    state_contract.validate_embedded_state(embedded, plan['manifest'])
    if json.loads(collection[core.STATE]) != embedded: raise RuntimeError('Persistent managed state differs')
    animation_witness = None
    if contract.is_animated(plan['manifest']):
        from toon_workflow_addon import animation_bpy, animation_state
        animation_witness = {'schema':animation_state.STATE_SCHEMA,
            'native':animation_bpy.capture_scope(plan['manifest']),
            'reference':animation_bpy.sample_scope(plan['manifest'], plan['reference_frame'])}
        if animation_witness != embedded['shading_animation']:
            raise RuntimeError('Cold native Action/reference witness differs')
    provenance = request.get('chain_provenance')
    if (type(provenance) is not list or any(type(row) is not dict or type(row.get('frame')) is not int for row in provenance)
            or [row['frame'] for row in provenance] != [row['frame'] for row in embedded['frames']]):
        raise RuntimeError('Cold full chain provenance frame domain differs')
    for row, frame in zip(provenance, embedded['frames']):
        fingerprint = frame['chain_fingerprint']
        if row.get('schema') != fingerprint['schema'] or row.get('sha256') != fingerprint['sha256']:
            raise RuntimeError('Cold full chain provenance fingerprint differs')
        chains = read_json(checked(row))
        if state_contract.chain_fingerprint(chains, plan['manifest']) != fingerprint:
            raise RuntimeError('Cold full chain provenance counts/canonical content differ')
        del chains
    protection = core.verify_protection(expectation, plan)
    if protection['status'] != 'passed': raise RuntimeError('Cold source protection failed: ' + str(protection['differences'][:4]))
    core._check_cache(collection, embedded)
    core.validate_group_styles(collection, plan['manifest'], embedded['frames'])
    if regions.is_region_mode(plan['manifest']['shading']):
        actual_materials = regions.snapshot(core.resolve_region_targets(plan), plan['targets'],
            plan['manifest']['shading'], plan['manifest']['engine'], plan['manifest']['scope_id'],
            animation_manifest=plan['manifest'] if contract.is_animated(plan['manifest']) else None)
    else:
        actual_materials = materials.managed_snapshot(plan['manifest']['scope_id'])
    if actual_materials != embedded['materials']: raise RuntimeError('Cold material state differs')
    for target in plan['targets']:
        obj = bpy.data.objects[target['name']]
        if core._source_digest(obj.data) != target['source_digest']: raise RuntimeError('Cold target content differs')
    start, end = plan['manifest']['time']['start'], plan['manifest']['time']['end']
    order = list(dict.fromkeys([end, start, (start + end) // 2, *range(end, start - 1, -1)]))
    visibility = []
    for frame in order:
        scene.frame_set(frame); bpy.context.view_layer.update()
        core.validate_render_contract(scene, plan['manifest'])
        actual = sorted(o.name for o in collection.objects if not o.hide_render)
        expected = sorted(embedded['frames'][frame - start]['objects'])
        if actual != expected: raise RuntimeError('Cold visibility differs at frame ' + str(frame))
        visible = {'frame': frame, 'visible_objects': actual, 'status': 'passed'}
        if contract.is_animated(plan['manifest']):
            visible['shading_sample'] = animation_bpy.sample_scope(plan['manifest'], frame)
            if visible['shading_sample'] != embedded['frames'][frame-start]['shading_sample']:
                raise RuntimeError('Cold actual shading values differ at frame '+str(frame))
        if contract.is_placed(plan['manifest']):
            bound_frame = embedded['frames'][frame - start]
            visible['placement_validation'] = core.validate_frame_placement(scene, collection, plan['manifest'], bound_frame)
            _validate_placement_validation(visible['placement_validation'], plan['manifest'], bound_frame, actual)
        visibility.append(visible)
    image_checks = _render_images(scene, plan, request) if render_images else []
    result = {'status': 'passed', 'candidate': request['candidate'], 'implementation_sha256': request['implementation_sha256'],
            'execution_context': actual_context, 'thread_observation': thread_observation(),
            'source_protection': protection, 'visibility': visibility, 'image_checks': image_checks,
            'render_settings': render_settings,
            'geometry': 'passed', 'materials': 'passed', 'chain_provenance': provenance,
            'chain_provenance_validation': 'full_canonical_fingerprint',
            'state_schema': state_contract.state_schema(plan['manifest']), 'source_text_executed': False,
            'limits': ['Integer-frame native cache only; image replay samples are not full animation aesthetics acceptance.']}
    if contract.is_animated(plan['manifest']):result['shading_animation'] = animation_witness
    return result


def _render_images(scene, plan, request):
    import bpy
    start, end = plan['manifest']['time']['start'], plan['manifest']['time']['end']
    image_checks = []
    if request['images']:
        import numpy as np
        from toon_workflow import render_frame
        folder = Path(request['output']).parent / 'toon-cold-frames'; folder.mkdir()
        by_frame = {r['frame']: r for r in request['images']}
        for frame in list(dict.fromkeys([end, start, (start + end) // 2])):
            reference = checked(by_frame[frame])
            cold = render_frame(scene, frame, folder / ('frame-%04d.png' % frame), plan['manifest'])
            if contract.is_animated(plan['manifest']) and cold['shading_sample'] != by_frame[frame]['shading_sample']:
                raise RuntimeError('Cold rendered shading sample differs from the warm bound frame')
            images = [bpy.data.images.load(str(p), check_existing=False) for p in (reference, Path(cold['file']))]
            try:
                if tuple(images[0].size) != tuple(images[1].size): raise RuntimeError('Cold image size differs')
                arrays = []
                for image in images:
                    pixels = np.empty(len(image.pixels), dtype=np.float32); image.pixels.foreach_get(pixels); arrays.append(pixels)
                delta = np.abs(arrays[0] - arrays[1])
                tolerance = 2 / 255 if plan['manifest']['engine'] == 'BLENDER_EEVEE' else 1 / 255
                largest = float(delta.max(initial=0))
                if not np.all(np.isfinite(delta)) or largest > tolerance:
                    raise RuntimeError('Cold rendered pixels differ at frame %s: %s' % (frame, largest))
                image_checks.append({'frame': frame,
                                     'reference': {k: by_frame[frame][k] for k in ('file', 'sha256', 'bytes')},
                                     'cold': {k: cold[k] for k in ('file', 'sha256', 'bytes')},
                                     'max_decoded_delta': largest, 'tolerance': tolerance, 'status': 'passed'})
            finally:
                for image in images: bpy.data.images.remove(image)
    return image_checks


def _guarded(row, stack):
    from filesystem import FileGuard
    _descriptor(row)
    path = checked(row)
    stack.enter_context(FileGuard(path))
    return checked(row)


def _images(request, stack):
    import bpy
    from toon_workflow_addon import core
    row = request['content_receipt']
    job = Path(request['candidate']['file']).resolve().parent
    expected = job / 'toon-content-reopen.json'
    names = {'candidate': 'toon-candidate.blend', 'state': 'toon-state.json',
             'plan': 'toon-plan.json', 'expectation': 'toon-expectation.json'}
    if (Path(row['file']) != expected or Path(request['output']).resolve().parent != job
            or any(Path(request[key]['file']) != job / names[key] for key in INPUT_KEYS)):
        raise RuntimeError('Content receipt is not the fixed owned job output')
    content = read_json(_guarded(row, stack))
    plan = read_json(checked(request['plan']))
    state = read_json(checked(request['state']))
    manifest = plan['manifest']
    if contract.is_placed(manifest):
        validate_content_receipt(content, request, manifest, state['embedded_state']['frames'], state['embedded_state'].get('shading_animation'), state['embedded_state']['materials'])
    else:
        validate_content_receipt(content, request, manifest)
    state_contract.validate_embedded_state(state['embedded_state'], manifest)
    expected_frames = list(range(manifest['time']['start'], manifest['time']['end'] + 1))
    if ([r['frame'] for r in content['visibility']] != list(dict.fromkeys([
            expected_frames[-1], expected_frames[0], (expected_frames[0] + expected_frames[-1]) // 2, *reversed(expected_frames)]))
            or [r['frame'] for r in request['images']] != (expected_frames if manifest['render']['enabled'] else [])
            or content['render_settings']['actual']['frames'] != [manifest['time']['start'], manifest['time']['end']]):
        raise RuntimeError('Content receipt is outside the sealed plan frame/render domain')
    for visible in content['visibility']:
        frame = state['embedded_state']['frames'][visible['frame'] - manifest['time']['start']]
        if (visible['visible_objects'] != sorted(frame['objects'])
                or content['chain_provenance'][frame['frame'] - manifest['time']['start']]['sha256'] != frame['chain_fingerprint']['sha256']):
            raise RuntimeError('Content receipt summaries differ from sealed state')
    bpy.ops.wm.open_mainfile(filepath=str(checked(request['candidate'])), load_ui=False, use_scripts=False)
    scene = bpy.data.scenes[manifest['scene']]; core.activate(scene)
    scene.frame_set(plan['reference_frame']); bpy.context.view_layer.update()
    if core.validate_render_contract(scene, manifest) != content['render_settings']:
        raise RuntimeError('Fresh image replay settings differ from sealed content')
    result = deepcopy(content)
    result.update(status='passed', reopen_schema='toon-reopen-combined-v2' if contract.is_placed(manifest) else 'toon-reopen-combined-v1',
                  image_checks=_render_images(scene, plan, request),
                  content_receipt=deepcopy(row), image_thread_observation=thread_observation())
    return result


def run(request):
    mode = request.get('verification_mode', 'full')
    if mode not in ('full', 'content', 'images'):
        raise RuntimeError('Unknown verification mode')
    if implementation_sha256() != request['implementation_sha256']:
        raise RuntimeError('Verifier implementation changed')
    from toon_workflow import execution_context
    expected_context = request.get('execution_context')
    _cpu_context(expected_context)
    actual_context = execution_context()
    if actual_context != expected_context:
        raise RuntimeError('Verifier CPU execution context differs: expected %s, actual %s' % (expected_context, actual_context))
    with ExitStack() as stack:
        for key in INPUT_KEYS:
            _guarded(request[key], stack)
        if mode == 'images':
            return _images(request, stack)
        result = _content(request, actual_context, render_images=mode == 'full')
        if mode == 'content':
            plan = read_json(checked(request['plan']))
            placed = contract.is_placed(plan['manifest'])
            result.update(status='content_passed', reopen_schema='toon-reopen-content-v2' if placed else 'toon-reopen-content-v1',
                          bound_inputs={key: deepcopy(request[key]) for key in INPUT_KEYS},
                          warm_images=deepcopy(request['images']))
            if placed:
                state = read_json(checked(request['state']))
                validate_content_receipt(result, request, plan['manifest'], state['embedded_state']['frames'], state['embedded_state'].get('shading_animation'), state['embedded_state']['materials'])
            else:
                validate_content_receipt(result, request, plan['manifest'])
            domain = list(range(plan['manifest']['time']['start'], plan['manifest']['time']['end'] + 1))
            if [r['frame'] for r in request['images']] != (domain if plan['manifest']['render']['enabled'] else []):
                raise RuntimeError('Content warm image inventory differs from sealed render manifest')
        return result


if __name__ == '__main__':
    request = read_json(sys.argv[sys.argv.index('--') + 1])
    try:
        atomic_json(request['output'], run(request))
    except Exception as exc:
        atomic_json(request['output'], {'status': 'failed', 'error': str(exc), 'traceback': traceback.format_exc(),
                                       'expected_execution_context': request.get('execution_context'),
                                       'thread_observation': thread_observation()})
        raise
