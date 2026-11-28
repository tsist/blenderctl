"""Transactional batch editing in a disposable Blender background worker."""
from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy

from . import core, retiming
from .batch_contract import normalize_batch_edit
from .batch_plan import build_groups, map_points
from .contract import FRAME_MAX, FRAME_MIN, WorkflowError, _clip_descriptors, validate_clip
from .protection import compare_protection, native_strip_record, snapshot_protection

TIMING = {'frame_start', 'content_start', 'content_end', 'frame_final_start', 'left_handle',
          'frame_final_end', 'right_handle', 'frame_final_duration', 'duration', 'frame_duration',
          'content_duration', 'frame_offset_start', 'left_handle_offset', 'frame_offset_end',
          'right_handle_offset'}
SOURCE_TIMING = {'animation_offset_start', 'content_trim_start', 'animation_offset_end',
                 'content_trim_end', 'sound_offset'}
BASELINES = ('_vwe_baseline', '_vwe_curves', '_vwe_native')


def _fail(code, message):
    raise WorkflowError(code, message)


def _markers(scene):
    return sorted((m.name, m.frame, m.camera.name if m.camera else '') for m in scene.timeline_markers)


def _roots(scene, workflow):
    owned = core._owned(scene, workflow)
    rows = []
    for strip in core._roots(scene):
        cid = strip.get('_vwe_clip_id') if strip.get('_vwe_workflow') == workflow else None
        spec = core._read(strip, '_vwe_spec', {}) if cid in owned else {}
        rows.append({'strip': strip, 'id': cid, 'name': strip.name, 'shot_id': spec.get('shot_id', cid),
                     'kind': spec.get('kind', strip.type), 'channel': strip.channel,
                     'start': int(strip.left_handle), 'end': int(strip.right_handle)})
    return rows


def _locked(scene, channel):
    # Native index zero is the reserved channel. RNA strip.channel starts at 1.
    return scene.sequence_editor and scene.sequence_editor.channels[channel].lock


def _children(strip):
    yield strip
    for child in getattr(strip, 'strips', ()):
        yield from _children(child)


def _belongs(path, prefixes):
    return any(path == prefix or path.startswith(prefix + '.') or path.startswith(prefix + '[')
               for prefix in prefixes)


def _action_curves(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', ()):
                for curve in bag.fcurves:
                    yield bag, curve


def _guard_references(scene, strip):
    internal = {s.as_pointer() for s in _children(strip)}
    if any(len(s.connections) for s in _children(strip)):
        _fail('UNSUPPORTED', 'Explicitly connected strips need a separate editorial decision: ' + strip.name)
    for other in scene.sequence_editor.strips_all:
        if other.as_pointer() in internal:
            continue
        references = [getattr(other, field, None) for field in ('input_1', 'input_2', 'input_3')]
        references.extend(mod.input_mask_strip for mod in other.modifiers)
        if any(ref and ref.as_pointer() in internal for ref in references):
            _fail('UNSUPPORTED', 'Edit is referenced by another strip or mask: ' + other.name)


def _guard_curves(scene, strip, slicing):
    ad = scene.animation_data
    if not ad:
        return
    prefixes = [s.path_from_id() for s in _children(strip)]
    if any(_belongs(curve.data_path, prefixes) for curve in ad.drivers):
        _fail('CONFLICT', 'A driver targets the edited strip: ' + strip.name)
    def nla_actions(strips):
        for item in strips:
            if item.action:
                yield item.action
            yield from nla_actions(item.strips)
    for action in (a for track in ad.nla_tracks for a in nla_actions(track.strips)):
        if any(_belongs(curve.data_path, prefixes) for _, curve in _action_curves(action)):
            _fail('CONFLICT', 'An NLA action targets the edited strip: ' + strip.name)
    if not ad.action:
        return
    active_paths = set(curve.as_pointer() for curve in core._curves(scene))
    relevant = [curve for _, curve in _action_curves(ad.action) if _belongs(curve.data_path, prefixes)]
    if relevant and (slicing or ad.action.users != 1 or
                     any(curve.as_pointer() not in active_paths for curve in relevant)):
        _fail('UNSUPPORTED', 'Cut crosses authored, shared or differently slotted animation: ' + strip.name)


def _capture(scene, row, *, slicing=False, deleting=False):
    strip, cid = row['strip'], row['id']
    if cid is None:
        _fail('UNSUPPORTED', 'Edit reaches an unmanaged strip: ' + strip.name)
    if strip.lock or _locked(scene, strip.channel):
        _fail('CONFLICT', 'Strip or track is locked: ' + cid)
    _guard_references(scene, strip)
    _guard_curves(scene, strip, slicing)
    if strip.type == 'META':
        if slicing:
            _fail('UNSUPPORTED', 'Cuts within a retimed strip need an explicit remap: ' + cid)
        retiming.inspect_mapping(scene, strip)
    elif getattr(strip, 'use_reverse_frames', False) or len(getattr(strip, 'retiming_keys', ())):
        _fail('UNSUPPORTED', 'Native reverse or retiming keys need an explicit remap: ' + cid)
    native = native_strip_record(strip)
    observed = (core._actual(scene, strip), core._curve_snapshot(scene, strip), native)
    prior = {key: {'baseline': core._read(strip, key), 'observed': value}
             for key, value in zip(BASELINES, observed)}
    spec, curves = observed[:2]
    baseline = prior['_vwe_baseline']['baseline'] or {}
    timing_baseline = (prior['_vwe_native']['baseline'] or {}).get('settings', {})
    if (any(not core._equal(spec.get(k), baseline.get(k)) for k in ('start', 'duration', 'channel', 'source')) or
            any(not core._equal(native['settings'].get(k), timing_baseline.get(k))
                for k in TIMING | SOURCE_TIMING)):
        _fail('CONFLICT', 'Manually changed timing, track or source: ' + cid)
    if not core._equal(curves, prior['_vwe_curves']['baseline']):
        _fail('CONFLICT', 'Manually changed strip animation: ' + cid)
    if deleting and (not core._equal(spec, baseline) or
                     not core._equal(native, prior['_vwe_native']['baseline'])):
        _fail('CONFLICT', 'Removal would discard manual strip changes: ' + cid)
    if not all(value['baseline'] is not None for value in prior.values()):
        _fail('CONFLICT', 'Missing managed strip baseline: ' + cid)
    return {**row, 'spec': spec, 'prior': prior}


def _right_id(operation, cid):
    digest = hashlib.sha256(f'{operation}\0{cid}'.encode()).hexdigest()[:16]
    return f'{operation[:32]}_r_{digest}'


def _slice(source, begin, end, delta, cid):
    spec = deepcopy(source['spec'])
    spec.update(id=cid, start=begin + delta, duration=end - begin)
    if cid != source['id']:
        spec['name'] = cid
    offset = begin - source['start']
    if offset and 'source' in spec:
        choices = [spec['source']] + [row['source'] for row in spec.get('alternatives', ())]
        for media in choices:
            if media['freeze'] or (spec['kind'] == 'IMAGE' and len(media['files']) == 1):
                continue
            if media['speed'] != 1:
                _fail('UNSUPPORTED', 'Clipping a variable-rate alternative needs a remap: ' + source['id'])
            media['source_in'] += offset
    return {'start': begin, 'end': end, 'delta': delta, 'spec': validate_clip(spec)}


def _check_resource(row):
    filename = Path(row['file'])
    if not filename.is_file():
        _fail('RESOURCE_MISSING', 'Missing inserted resource: ' + str(filename))
    if core._sha(filename) != row['expected_sha256']:
        _fail('CONFLICT', 'Inserted resource SHA changed: ' + str(filename))


def _verify_insert_resources(spec):
    for resource in _clip_descriptors(spec):
        _check_resource(resource)


def _layout(roots, modified, inserted):
    changed, untouched = defaultdict(list), defaultdict(list)
    for row in roots:
        if row['id'] not in modified:
            untouched[row['channel']].append((row['start'], row['end'], row['name']))
    all_parts = [part['spec'] for source in modified.values() for part in source['parts']] + inserted
    unchanged = [row for row in roots if row['id'] not in modified]
    ids = {row['id'] for row in unchanged}
    names = {row['name'] for row in unchanged}
    for strip in all_parts:
        if strip['id'] in ids or strip['name'] in names:
            _fail('CONFLICT', 'Strip name or identity already exists: ' + strip['id'])
        ids.add(strip['id'])
        names.add(strip['name'])
        start, end = strip['start'], strip['start'] + strip['duration']
        for a, b, name in untouched[strip['channel']] + changed[strip['channel']]:
            if start < b and a < end:
                _fail('CONFLICT', f'{strip["id"]} would overlap {name} on channel {strip["channel"]}')
        changed[strip['channel']].append((start, end, strip['name']))
    return all_parts


def _prepare(scene, workflow, row, points):
    kind = row['op']
    start = points.get('at', points.get('start'))
    end = points.get('end', start)
    timeline = core._timeline(scene)
    if (start < max(FRAME_MIN, timeline['frame_start']) or start > timeline['frame_end_exclusive'] or
            end > min(FRAME_MAX + 1, timeline['frame_end_exclusive']) or
            (kind == 'remove' and end <= start)):
        _fail('INVALID_REQUEST', 'Edit points must form a valid range in the current timeline')
    for channel in row['channels']:
        if _locked(scene, channel):
            _fail('CONFLICT', 'Track is locked: ' + str(channel))
    ripple = row.get('ripple', False)
    delta = (row['duration'] if kind == 'insert' else start - end) if ripple else 0
    roots = _roots(scene, workflow)
    modified, inserted = {}, []
    cut_points = []
    for root in roots:
        if root['channel'] not in row['channels']:
            continue
        a, b, cid = root['start'], root['end'], root['id']
        portions = None
        if kind == 'remove' and a < end and start < b:
            portions = [(a, start, 0)] if a < start else []
            if end < b:
                portions.append((end, b, delta))
            cut_points.extend(cut for cut in (start, end) if a <= cut <= b)
        elif (kind == 'split' or (kind == 'insert' and ripple)) and a < start < b:
            portions = [(a, start, 0), (start, b, delta)]
            cut_points.append(start)
        elif delta and a >= end:
            portions = [(a, b, delta)]
        if portions is None:
            continue
        slicing = any(left != a or right != b for left, right, _ in portions)
        source = _capture(scene, root, slicing=slicing, deleting=not portions)
        source['parts'] = [_slice(source, left, right, shift, cid if index == 0 else _right_id(row['id'], cid))
                           for index, (left, right, shift) in enumerate(portions)]
        modified[cid] = source
    if kind != 'insert' and not modified:
        _fail('NOT_FOUND', 'No editable strip reaches the requested cut')
    inserted_end = start
    if kind == 'insert':
        inserted_end = start + row['duration']
        if inserted_end > FRAME_MAX + 1:
            _fail('INVALID_REQUEST', 'Inserted interval exceeds native frame limits')
        for raw in row['clips']:
            spec = validate_clip({**raw, 'start': start})
            if spec['channel'] not in row['channels'] or spec['duration'] > row['duration']:
                _fail('INVALID_REQUEST', 'Inserted clip is outside the selected tracks or duration: ' + spec['id'])
            if spec['id'] in modified:
                _fail('CONFLICT', 'Inserted clip identity already exists: ' + spec['id'])
            _verify_insert_resources(spec)
            inserted.append(spec)
    all_parts = _layout(roots, modified, inserted)
    timeline_end = max([timeline['frame_end_exclusive'], inserted_end] +
                       [s['start'] + s['duration'] for s in all_parts])
    extend_timeline = timeline_end != timeline['frame_end_exclusive']
    if extend_timeline:
        baseline_end = core._read(scene, '_vwe_timeline', {}).get('frame_end_exclusive')
        if timeline['frame_end_exclusive'] != baseline_end:
            _fail('CONFLICT', 'Extending the timeline would overlap a manual scene range change')
    return {'modified': modified, 'inserted': inserted,
            'timeline': {**timeline, 'frame_end_exclusive': timeline_end},
            'extend_timeline': extend_timeline,
            'history': {'index': row['index'], 'start': start, 'end': end, 'delta': delta},
            'result': {'start': start, 'end': inserted_end if kind == 'insert' else
                       (start if delta < 0 else end)},
            'cuts': sorted(set(cut_points))}


def _static(record, fade):
    ignored = TIMING | {'name'}
    if fade:
        ignored.add('volume' if record['type'] == 'SOUND' else 'blend_alpha')
    if record['type'] == 'SPEED':
        # The managed mapping owns its animated sample. Protection also
        # serializes its input pointer as nested RNA, including input timing.
        ignored.add('speed_frame_number')
    settings = {key: value for key, value in record['settings'].items() if key not in ignored}
    if record['type'] == 'SPEED' and 'input_1' in settings:
        settings['input_1'] = {key: value for key, value in settings['input_1'].items() if key not in TIMING}
    return {**{key: value for key, value in record.items() if key not in
               ('name', 'settings', 'frame_final_start', 'frame_final_end', 'children')},
            'settings': settings,
            'children': [_static(child, False) for child in record.get('children', ())]}


def _erase(scene, strip):
    prefixes = [s.path_from_id() for s in _children(strip)]
    ad = scene.animation_data
    if ad and ad.action:
        for bag, curve in list(_action_curves(ad.action)):
            if _belongs(curve.data_path, prefixes):
                bag.fcurves.remove(curve)
    scene.sequence_editor.strips.remove(strip)


def _crop(strip, part):
    start, end, spec = part['start'], part['end'], part['spec']
    if int(strip.left_handle) != start:
        strip.left_handle = start
    if int(strip.right_handle) != end:
        strip.right_handle = end
    # A held still has no source offset in the existing source contract.
    if spec['kind'] == 'IMAGE' and len(spec['source']['files']) == 1 and strip.type != 'META':
        strip.content_start = start
        strip.left_handle, strip.right_handle = start, end


def _edit_native(scene, plan, workflow):
    moved, retained = [], []
    for source in plan['modified'].values():
        if not source['parts']:
            _erase(scene, source['strip'])
    for source in plan['modified'].values():
        strip, parts = source['strip'], source['parts']
        if not parts:
            continue
        native = [strip]
        if len(parts) == 2:
            try:
                right = strip.split(parts[1]['start'], 'SOFT', ignore_connections=True)
            except RuntimeError as error:
                _fail('VALIDATION_FAILED', f'Native split failed for {strip.name}: {error}')
            if right is None:
                _fail('VALIDATION_FAILED', 'Native split did not produce a right strip: ' + strip.name)
            right.name = parts[1]['spec']['name']
            right['_vwe_clip_id'] = parts[1]['spec']['id']
            native.append(right)
        for obj, part in zip(native, parts):
            if part['start'] != source['start'] or part['end'] != source['end']:
                _crop(obj, part)
            (moved if part['delta'] else retained).append((obj, part, source))
    moved.sort(key=lambda item: item[1]['start'], reverse=plan['history']['delta'] > 0)
    for obj, part, source in moved:
        spec = part['spec']
        if obj.type == 'META':
            retiming.update_mapping(scene, obj, spec['start'], spec['duration'])
        else:
            obj.content_start += part['delta']
        if spec['fade_in'] or spec['fade_out']:
            core._style(scene, obj, spec, {'start'})
    for obj, part, source in retained + moved:
        spec = part['spec']
        fade = spec['fade_in'] or spec['fade_out']
        if not core._equal(_static(native_strip_record(obj), fade),
                           _static(source['prior']['_vwe_native']['observed'], fade)):
            _fail('VALIDATION_FAILED', 'Native edit changed non-timing strip settings: ' + spec['id'])
        core._store(scene, obj, spec, source['prior'])
        core._assert_fields(scene, obj, spec, spec)
    cache = {}
    for spec in plan['inserted']:
        core._make(scene, spec, workflow, font_cache=cache)
    if plan['extend_timeline']:
        end = plan['timeline']['frame_end_exclusive']
        scene.frame_end = end - 1
        stored = core._read(scene, '_vwe_timeline')
        scene['_vwe_timeline'] = core._json({**stored, 'frame_end_exclusive': end})
    if not core._equal(core._timeline(scene), plan['timeline']):
        _fail('VALIDATION_FAILED', 'Native playback range differs from the planned result')


def _execute(scene, workflow, plan):
    changed = plan['modified']
    names = [s['name'] for s in changed.values()]
    for source in changed.values():
        names.extend(part['spec']['name'] for part in source['parts'])
    names.extend(s['name'] for s in plan['inserted'])
    paths = [strip.path_from_id() for source in changed.values() for strip in _children(source['strip'])]
    same_markers = _markers(scene)
    before = snapshot_protection(scene.name, names, paths)
    _edit_native(scene, plan, workflow)
    after = snapshot_protection(scene.name, names, paths)
    witness = compare_protection(before, after)
    if witness['status'] != 'pass' or _markers(scene) != same_markers:
        _fail('VALIDATION_FAILED', 'The edit changed protected project contents: ' + str(witness['differences']))
    return witness


def _save(path):
    if bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False, copy=True,
                                  relative_remap=True) != {'FINISHED'} or not path.is_file():
        _fail('WORKER_FAILED', 'Could not create a VSE transaction checkpoint')


def _restore(path, name, witness, markers):
    if bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False) != {'FINISHED'}:
        _fail('WORKER_FAILED', 'Could not restore the VSE transaction checkpoint')
    if snapshot_protection(name) != witness or _markers(bpy.data.scenes[name]) != markers:
        _fail('VALIDATION_FAILED', 'Restored checkpoint differs from its pre-edit expectation')


def _report(row, **fields):
    targets = ('start', 'end') if row['op'] == 'remove' else ('at',)
    return {'id': row['id'], 'op': row['op'], 'channels': row['channels'],
            'requested': {name: row[name] for name in targets}, 'submitted': row['submitted'], **fields}


def run(request, *, checkpoint_dir):
    """Edit an open disposable file, using disk savepoints for native rollback.

    The caller owns source locking, final save and an independent cold reopen.
    This entry must run in a background worker and never saves to the source.
    """
    request = normalize_batch_edit(request)
    if not bpy.app.background:
        _fail('UNSUPPORTED', 'Batch editing requires an isolated Blender background worker')
    directory = Path(checkpoint_dir)
    if not directory.is_absolute() or not directory.is_dir():
        _fail('INVALID_REQUEST', 'An existing absolute transaction directory is required')
    name, workflow = request['scene'], request['workflow_id']
    scene = bpy.data.scenes.get(name)
    if not scene or scene.get('_vwe_workflow') != workflow:
        _fail('CONFLICT', 'Batch target is not this workflow Scene')
    roots = [{key: value for key, value in row.items() if key != 'strip'} for row in _roots(scene, workflow)]
    markers = defaultdict(list)
    for marker, frame, _ in _markers(scene):
        markers[marker].append(frame)
    groups = build_groups(request['operations'], request['atomicity'], roots, markers)
    changes, results, reports, group_reports = defaultdict(list), {}, {}, []
    # Keep rollback files alive until all possible opens have completed.
    with TemporaryDirectory(prefix='vse-batch-', dir=directory) as workspace:
        for index, group in enumerate(groups, 1):
            scene = bpy.data.scenes[name]
            group_changes = defaultdict(list, {key: list(value) for key, value in changes.items()})
            group_results = dict(results)
            members = [row['id'] for row in group]
            pending, attempted = [], {}
            invalid = next((row for row in group if 'error' in row), None)
            failure = invalid['error'] if invalid else None
            failed_id = invalid['id'] if invalid else None
            checkpoint = Path(workspace) / f'group-{index}.blend'
            checkpoint_witness = checkpoint_markers = None
            try:
                for row in (() if invalid else group):
                    failed_id = row['id']
                    points = map_points(row, group_changes, group_results)
                    attempted[row['id']] = points
                    plan = _prepare(scene, workflow, row, points)
                    if checkpoint_witness is None:
                        checkpoint_witness, checkpoint_markers = snapshot_protection(name), _markers(scene)
                        _save(checkpoint)
                    witness = _execute(scene, workflow, plan)
                    if plan['history']['delta']:
                        for channel in row['channels']:
                            group_changes[channel].append(plan['history'])
                    group_results[row['id']] = {'index': row['index'], 'result': plan['result']}
                    pending.append(_report(row, status='committed', actual=points, result=plan['result'],
                                           cut_frames=plan['cuts'], delta=plan['history']['delta'],
                                           modified={source['id']: [part['spec']['id'] for part in source['parts']]
                                                     for source in plan['modified'].values()},
                                           strip_changes=_strip_changes(plan),
                                           inserted=[s['id'] for s in plan['inserted']], protection=witness))
            except Exception as error:
                if checkpoint.is_file():
                    _restore(checkpoint, name, checkpoint_witness, checkpoint_markers)
                if not isinstance(error, WorkflowError):
                    raise
                failure = {'code': error.code, 'message': str(error)}
            if failure:
                pending_ids = {item['id'] for item in pending}
                for row in group:
                    status = 'conflict' if row['id'] == failed_id else ('rolled_back' if row['id'] in pending_ids else 'skipped')
                    fields = {'error': failure} if status == 'conflict' else {}
                    if row['id'] in attempted:
                        fields['attempted'] = attempted[row['id']]
                    reports[row['id']] = _report(row, status=status, **fields)
                group_reports.append({'members': members, 'status': 'rolled_back', 'failed_at': failed_id,
                                      'restored_checkpoint': checkpoint.is_file(), 'error': failure})
            else:
                changes, results = group_changes, group_results
                reports.update((item['id'], item) for item in pending)
                group_reports.append({'members': members, 'status': 'committed'})
    committed = sum(row['status'] == 'committed' for row in group_reports)
    return {'version': '1.0', 'scene': name, 'workflow_id': workflow, 'atomicity': request['atomicity'],
            'status': 'committed' if committed == len(groups) else 'partial' if committed else 'rejected',
            'groups': group_reports, 'operation_log': [reports[row['id']] for row in request['operations']],
            'timeline': core._timeline(bpy.data.scenes[name])}


def _strip_changes(plan):
    def output(spec):
        return {'id': spec['id'], 'channel': spec['channel'], 'start': spec['start'],
                'end': spec['start'] + spec['duration']}
    rows = [{'before': {key: row[key] for key in ('id', 'channel', 'start', 'end')},
             'after': [output(part['spec']) for part in row['parts']]}
            for row in plan['modified'].values()]
    rows.extend({'before': None, 'after': [output(spec)]} for spec in plan['inserted'])
    return rows
