"""Owned PNG IMAGE proxy preview; parent Scene/candidate never mutated.

Explicit media substitution in an independent disk-candidate process, not
native StripProxy/viewport rendering. Movie/VFR and HDR preview are rejected.
"""
import copy
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import Failure, atomic_json, digest, read_json
from video_identity import implementation_sha256, runtime_documents
from video_workflow_contract import descriptor


def _png_size(path):
    with Path(path).open('rb') as stream:
        header = stream.read(33)
    if len(header) != 33 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise Failure('UNSUPPORTED', 'Proxy preview currently requires PNG IMAGE source/proxy members')
    dimensions = struct.unpack('>II', header[16:24])
    if min(dimensions) < 1:
        raise Failure('INVALID_REQUEST', 'Invalid PNG proxy dimensions')
    return dimensions


def _clips(report):
    inspection = report.get('saved_expectation', {}).get('inspection') or report.get('inspection')
    if not isinstance(inspection, dict) or not isinstance(inspection.get('clips'), list):
        raise Failure('INVALID_REQUEST', 'Proxy preview requires actual saved candidate inspection')
    rows = {}
    for value in inspection['clips']:
        row = copy.deepcopy(value)
        if isinstance(row.get('actual'), dict):
            row.update(row['actual'])
        if row.get('id') in rows:
            raise Failure('CONFLICT', 'Duplicate inspected clip identity')
        rows[row.get('id')] = row
    return inspection, rows


def _plan(params, report):
    output = params.get('output', {})
    if output.get('color_mode', 'SDR') != 'SDR':
        raise Failure('UNSUPPORTED', 'HDR proxy preview is not qualified')
    preview = output.get('preview')
    if not isinstance(preview, dict) or set(preview) != {'proxies'} or not isinstance(preview['proxies'], list) or not 1 <= len(preview['proxies']) <= 4096:
        raise Failure('INVALID_REQUEST', 'preview requires 1..4096 explicit proxies')
    inspection, clips = _clips(report)
    resources = {descriptor(row)['file'].casefold(): descriptor(row) for row in params['resources']}
    seen = set(); result = []
    for row in preview['proxies']:
        if not isinstance(row, dict) or set(row) != {'clip_id', 'source_files', 'files'} or not isinstance(row['clip_id'], str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}', row['clip_id']):
            raise Failure('INVALID_REQUEST', 'Proxy entry requires clip_id/source_files/files')
        if row['clip_id'] in seen:
            raise Failure('INVALID_REQUEST', 'Duplicate proxy clip_id')
        seen.add(row['clip_id'])
        clip = clips.get(row['clip_id'])
        if clip is None:
            raise Failure('NOT_FOUND', 'Proxy clip_id is absent from candidate')
        if clip.get('kind') != 'IMAGE':
            raise Failure('UNSUPPORTED', 'Only PNG IMAGE proxies are qualified; MOVIE/VFR explicitly rejected')
        if not isinstance(row['source_files'], list) or not isinstance(row['files'], list) or not 1 <= len(row['files']) <= 100000 or len(row['source_files']) != len(row['files']):
            raise Failure('INVALID_REQUEST', 'IMAGE proxy members must match complete source member count/order')
        original = [descriptor(value) for value in clip.get('source', {}).get('files', [])]
        supplied = [descriptor(value) for value in row['source_files']]
        files = [descriptor(value) for value in row['files']]
        if [(r['file'].casefold(), r['expected_sha256']) for r in supplied] != [(r['file'].casefold(), r['expected_sha256']) for r in original]:
            raise Failure('CONFLICT', 'Proxy source_files differ from original candidate inspection')
        sizes = []; small = []
        for source, proxy in zip(supplied, files):
            for value in (source, proxy):
                if resources.get(value['file'].casefold()) != value:
                    raise Failure('CONFLICT', 'Proxy/source descriptor missing from resources or SHA differs')
            sizes.append(_png_size(source['file'])); small.append(_png_size(proxy['file']))
        if len(set(sizes)) != 1 or len(set(small)) != 1:
            raise Failure('UNSUPPORTED', 'Mixed sequence member dimensions are not qualified')
        width, height = sizes[0]; proxy_width, proxy_height = small[0]
        if width * proxy_height != height * proxy_width or proxy_width >= width or proxy_height >= height:
            raise Failure('INVALID_REQUEST', 'Proxy must have lower resolution and exactly the same aspect')
        if len({str(Path(r['file']).parent).casefold() for r in files}) != 1:
            raise Failure('UNSUPPORTED', 'Proxy IMAGE sequence must have one explicit directory')
        result.append({'clip_id': row['clip_id'], 'source_files': supplied, 'files': files,
                       'source_dimensions': [width, height], 'proxy_dimensions': [proxy_width, proxy_height],
                       'scale_x': width / proxy_width, 'scale_y': height / proxy_height,
                       'frame_count': len(files), 'time_mapping': 'ordered IMAGE index identity; original integer project frames/FPS/retiming unchanged'})
    return inspection, result


def _input_rows(params, report):
    rows = list(params['resources']) + runtime_documents()
    rows.append({'file': report['candidate']['file'], 'expected_sha256': report['candidate']['sha256']})
    seen = {}
    for value in rows:
        row = descriptor(value); key = row['file'].casefold()
        if key in seen and seen[key]['expected_sha256'] != row['expected_sha256']:
            raise Failure('CONFLICT', 'Conflicting proxy input SHA')
        seen[key] = row
    return list(seen.values())


def _check_rows(rows):
    for row in rows:
        if digest(row['file']) != row['expected_sha256']:
            raise Failure('CONFLICT', 'Proxy input changed: ' + row['file'])


def render_proxy_outputs(params, job, report, progress):
    """Parent only guards/launches/reads; never accesses or mutates Scene data."""
    import bpy
    from filesystem import FileGuard
    from processes import ProcessTree
    job = Path(job).resolve()
    if not job.is_dir():
        raise Failure('NOT_FOUND', 'Owning proxy job directory is absent')
    # Keep owned nesting compact for Windows paths; mkdir refuses a collision.
    root = job / ('vp-' + uuid.uuid4().hex[:16]); root.mkdir(exist_ok=False)
    config_path = root / 'worker-request.json'; result_path = root / 'worker-result.json'
    rows = _input_rows(params, report)
    tree = None
    with ExitStack() as stack:
        guards = [stack.enter_context(FileGuard(row['file'])) for row in rows]
        for row, guard in zip(rows, guards):
            if guard.sha256() != row['expected_sha256']:
                raise Failure('CONFLICT', 'Proxy source/resource/implementation SHA mismatch')
        inspection, plan = _plan(params, report)
        current = implementation_sha256()
        if params.get('implementation_sha256', current) != current:
            raise Failure('CONFLICT', 'Proxy implementation differs from submitted request')
        python = Path(sys.executable)
        if python.name.lower() != 'python.exe':
            python = Path(bpy.app.binary_path).parent / (str(bpy.app.version[0]) + '.' + str(bpy.app.version[1])) / 'python/bin/python.exe'
        if not python.is_file():
            raise Failure('UNSUPPORTED', 'Owned preview requires the existing project Python runtime')
        child_job = root / 'render-job'; child_job.mkdir(exist_ok=False)
        atomic_json(config_path, {'params': params, 'report': {'candidate': report['candidate'],
            'runtime': report.get('runtime'), 'saved_expectation': {'inspection': inspection}},
            'plan': plan, 'inputs': rows, 'implementation_sha256': current,
            'child_job': str(child_job), 'parent_job': str(job), 'result': str(result_path)})
        gate = root / 'owned-gate'; gate.mkdir(exist_ok=False)
        command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
            '--offline-mode', '--threads', '2', '--python-exit-code', '23', '--python', str(Path(__file__).resolve()),
            '--', str(config_path)]
        timeout = read_json(job / 'launch.json').get('timeout_seconds', 180) if (job / 'launch.json').is_file() else 180
        started = time.monotonic()
        try:
            with (root / 'stdout.log').open('wb') as stdout, (root / 'stderr.log').open('wb') as stderr:
                env = os.environ.copy(); env['PYTHONDONTWRITEBYTECODE'] = '1'
                tree = ProcessTree([str(python), '-B', str(Path(__file__).with_name('bootstrap.py')), str(gate), *command],
                    job_name='blenderctl-proxy-' + uuid.uuid4().hex, stdout=stdout, stderr=stderr, env=env)
                progress('proxy_child_launch', total=len(plan))
                (gate / 'worker.gate').write_text('owned tree assigned', encoding='utf-8')
                while tree.process.poll() is None:
                    if (job / 'cancel.request').exists():
                        raise Failure('CANCELLED', 'Proxy preview cancelled')
                    if time.monotonic() - started > timeout:
                        raise Failure('TIMEOUT', 'Owned proxy preview exceeded launch budget')
                    child_progress = read_json(root / 'worker-progress.json') if (root / 'worker-progress.json').is_file() else {}
                    progress('proxy_preview', **{key: child_progress[key] for key in ('completed', 'total', 'frame', 'shot_id') if key in child_progress})
                    time.sleep(.2)
                result = read_json(result_path) if result_path.is_file() else {}
                if tree.process.returncode != 0 or not result.get('ok'):
                    error = result.get('error', {})
                    raise Failure(error.get('code', 'WORKER_FAILED'), error.get('message', 'Proxy worker exited without passed evidence'))
        finally:
            if tree is not None:
                if tree.process.poll() is None:
                    (root / 'cancel.request').write_text('owned parent stopped', encoding='utf-8')
                    tree.terminate()
                tree.close()
        _check_rows(rows)
        output = result['data']
        output['proxy_worker_report'] = {'file': str(result_path), 'sha256': digest(result_path)}
        output['parent_candidate_unchanged'] = True
        return output


def _curves(scene):
    ad = scene.animation_data
    if ad is None or ad.action is None:
        return []
    return [curve for layer in ad.action.layers for strip in layer.strips for bag in strip.channelbags
            if bag.slot_handle == ad.action_slot_handle for curve in bag.fcurves]


def _private_action(scene):
    ad = scene.animation_data
    if ad is None:
        return None
    if any(not track.mute and any(not strip.mute for strip in track.strips) for track in ad.nla_tracks):
        raise Failure('UNSUPPORTED', 'NLA proxy compensation is not qualified')
    if ad.action is None:
        return None
    if getattr(ad.action, 'is_action_legacy', False):
        raise Failure('UNSUPPORTED', 'Legacy Action proxy compensation is not qualified')
    identifier = ad.action_slot.identifier if ad.action_slot else None
    original = ad.action
    clone = original.copy(); ad.action = clone
    if identifier:
        matches = [slot for slot in clone.slots if slot.identifier == identifier]
        if len(matches) != 1:
            raise Failure('CONFLICT', 'Private proxy Action slot differs')
        ad.action_slot = matches[0]
    return {'original': original.name, 'private_copy': clone.name}


def _compensate(scene, media, plan):
    factors = [(media.transform, 'scale_x', plan['scale_x'], False),
               (media.transform, 'scale_y', plan['scale_y'], False),
               (media.crop, 'min_x', 1 / plan['scale_x'], True),
               (media.crop, 'max_x', 1 / plan['scale_x'], True),
               (media.crop, 'min_y', 1 / plan['scale_y'], True),
               (media.crop, 'max_y', 1 / plan['scale_y'], True)]
    changes = []
    for owner, field, factor, integer in factors:
        path = owner.path_from_id(field)
        ad = scene.animation_data
        if ad and any(curve.data_path == path for curve in ad.drivers):
            raise Failure('UNSUPPORTED', 'Driven proxy scale/crop compensation is not qualified')
        curves = [curve for curve in _curves(scene) if curve.data_path == path]
        if len(curves) > 1:
            raise Failure('CONFLICT', 'Ambiguous proxy compensation curve')
        old = getattr(owner, field); value = old * factor
        if integer and not math.isclose(value, round(value), abs_tol=1e-6):
            raise Failure('UNSUPPORTED', 'Crop pixels are not representable at proxy resolution')
        if curves:
            curve = curves[0]
            if curve.modifiers:
                raise Failure('UNSUPPORTED', 'Modified proxy compensation curves are not qualified')
            for point in curve.keyframe_points:
                if integer and (point.interpolation != 'CONSTANT' or not math.isclose(point.co.y * factor, round(point.co.y * factor), abs_tol=1e-6)):
                    raise Failure('UNSUPPORTED', 'Animated crop requires exactly representable CONSTANT keys')
                point.co.y *= factor; point.handle_left.y *= factor; point.handle_right.y *= factor
            curve.update()
        setattr(owner, field, int(round(value)) if integer else value)
        changes.append({'path': path, 'factor': factor, 'animated': bool(curves)})
    return changes


def _worker(config_path):
    import bpy
    from filesystem import FileGuard
    from video_editing_workflow_addon import core
    from video_outputs import render_outputs
    config_path = Path(config_path).resolve(); root = config_path.parent
    config = read_json(config_path)
    result_path = Path(config['result'])
    if result_path.resolve() != root / 'worker-result.json' or Path(config['child_job']).resolve() != root / 'render-job':
        raise Failure('INVALID_REQUEST', 'Proxy worker output leaves owning path')
    params = config['params']; candidate = config['report']['candidate']
    def progress(stage, **extra):
        if (Path(config['parent_job']) / 'cancel.request').exists() or (root / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Owned proxy child cancelled')
        atomic_json(root / 'worker-progress.json', {'stage': stage, **extra})
    try:
        with ExitStack() as stack:
            guards = [stack.enter_context(FileGuard(row['file'])) for row in config['inputs']]
            for row, guard in zip(config['inputs'], guards):
                if guard.sha256() != row['expected_sha256']:
                    raise Failure('CONFLICT', 'Proxy child input SHA changed')
            if implementation_sha256() != config['implementation_sha256']:
                raise Failure('CONFLICT', 'Proxy child implementation changed')
            bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False, use_scripts=False)
            scene = bpy.data.scenes[params['manifest']['scene']]
            bpy.context.window.scene = scene; bpy.context.window.view_layer = scene.view_layers[0]
            plan_report = []; action_copy = _private_action(scene)
            for plan in config['plan']:
                progress('proxy_relink', shot_id=plan['clip_id'])
                matches = [strip for strip in scene.sequence_editor.strips_all if strip.get('_vwe_clip_id') == plan['clip_id']]
                if len(matches) != 1:
                    raise Failure('CONFLICT', 'Proxy owner strip identity differs')
                owner = matches[0]
                members = [owner] if owner.type == 'IMAGE' else [strip for strip in owner.strips if strip.type == 'IMAGE'] if owner.type == 'META' else []
                if len(members) != 1:
                    raise Failure('UNSUPPORTED', 'Proxy requires one direct IMAGE media member')
                media = members[0]
                if len(media.elements) != len(plan['files']):
                    raise Failure('CONFLICT', 'Actual IMAGE member count differs')
                old_paths = [str((Path(bpy.path.abspath(media.directory)) / element.filename).resolve()) for element in media.elements]
                if [value.casefold() for value in old_paths] != [value['file'].casefold() for value in plan['source_files']]:
                    raise Failure('CONFLICT', 'Actual IMAGE paths differ from original inspection')
                for file in plan['files']:
                    image = bpy.data.images.load(file['file'], check_existing=False)
                    try:
                        if list(image.size) != plan['proxy_dimensions'] or len(image.pixels) != plan['proxy_dimensions'][0] * plan['proxy_dimensions'][1] * 4:
                            raise Failure('VALIDATION_FAILED', 'Proxy member decode/dimensions failed')
                        image.pixels[0]  # force data access before the actual sequence render
                    finally:
                        bpy.data.images.remove(image)
                media.use_proxy = False
                media.directory = str(Path(plan['files'][0]['file']).parent) + os.sep
                for element, row in zip(media.elements, plan['files']):
                    element.filename = Path(row['file']).name
                compensation = _compensate(scene, media, plan)
                for strip in scene.sequence_editor.strips_all:
                    strip.invalidate_cache('RAW'); strip.invalidate_cache('COMPOSITE')
                bpy.context.evaluated_depsgraph_get().update()
                plan_report.append({**plan, 'compensation': compensation})
            child_params = copy.deepcopy(params); child_params['output'].pop('preview')
            inspection = core.inspect(scene.name, params['manifest']['workflow_id'])
            child_report = {'candidate': candidate, 'inspection': inspection, 'saved_expectation': {'inspection': inspection}}
            output = render_outputs(child_params, Path(config['child_job']), child_report, progress)
            if output.get('status') != 'passed':
                raise Failure('VALIDATION_FAILED', 'Proxy output did not pass')
            _check_rows(config['inputs'])
            output.update(preview_kind='LOW_RESOLUTION_IMAGE_PROXY', original_quality=False,
                          input_candidate={'file': candidate['file'], 'expected_sha256': candidate['sha256']}, original_media_identity=plan_report,
                          action_compensation=action_copy, candidate_saved=False,
                          scope='PNG IMAGE only, same-aspect media index mapping; private Action compensation; no Movie/VFR/HDR or original-quality acceptance')
            atomic_json(result_path, {'ok': True, 'data': output})
        return 0
    except Exception as exc:
        atomic_json(result_path, {'ok': False, 'error': {'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc)}})
        return 1


if __name__ == '__main__':
    sys.exit(_worker(sys.argv[sys.argv.index('--') + 1]))
