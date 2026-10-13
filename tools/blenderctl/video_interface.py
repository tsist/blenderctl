"""Cheap video discovery and bounded evidence projections; full reports persist."""
from pathlib import Path
from protocol import Failure, VERSION, digest, read_json


def descriptor(path):
    return {'file': str(path), 'sha256': digest(path)}


def describe(operation=None, schema=False, section=None):
    from video_workflow_contract import schemas
    from video_editing_workflow_addon import contract
    available = schemas()
    if schema and section:
        raise Failure('INVALID_REQUEST', 'Choose schema or section')
    if section:
        properties = available.get(operation or 'manifest', {}).get('properties', {})
        if section not in properties:
            raise Failure('INVALID_REQUEST', 'Unknown video contract section')
        return {'operation': operation or 'manifest', 'section': section, 'schema': properties[section]}
    if schema:
        if operation not in available:
            raise Failure('INVALID_REQUEST', '--schema requires a video operation')
        return {'operation': operation, 'schema': available[operation]}
    return {'cli_version': VERSION, 'addon_version': '0.1-dev', 'video_schema_version': '1.0',
            'operations': ['video.run', 'video.inspect'], 'actions': ['create', 'patch'],
            'state_source': 'SAVED_DISK_V1; explicit GUI saved snapshot only',
            'contract': descriptor(Path(contract.__file__)),
            'compact': '--compact request/video run/video inspect/job status/result/wait; full report persists',
            'support': {'implementation': 'development candidate; capability presence is not acceptance',
                        'HDR10_PQ': 'STANDARD_100/REQUIRE_OPAQUE technical candidate; explicit SHA-bound OCIO and virtual mastering; device viewing not_run',
                        'visual_quality': 'requires actual output observation', 'audio_quality': 'requires actual listening',
                        'HDR_device_viewing': 'not_run'},
            'verification': 'source/resources SHA; saved expectation; independent Blender reader; output evidence when requested'}


def progress_summary(value):
    if not isinstance(value, dict):
        raise Failure('INVALID_REQUEST', 'Video progress must be an object')
    keys = ('stage', 'status', 'seconds', 'completed', 'total', 'shot_id', 'frame', 'candidate', 'error', 'recovery')
    return {key: value[key] for key in keys if key in value}


def compact(result, requested_command=None):
    root_value = (result.get('artifacts') or {}).get('job_directory')
    root = Path(root_value) if root_value else None
    command = requested_command or result.get('command')
    if root and (root / 'request.json').is_file():
        command = read_json(root / 'request.json')['command']
    if command not in ('video.run', 'video.inspect'):
        raise Failure('INVALID_REQUEST', 'Not a video job')
    out = {key: result[key] for key in ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error') if key in result}
    report_path = root / 'video-report.json' if root else None
    report = read_json(report_path) if report_path and report_path.is_file() else result.get('data') or {}
    keys = ('status', 'source', 'candidate', 'implementation_sha256', 'protection', 'recovery', 'checks', 'metrics', 'error', 'support', 'signature', 'resumed_from')
    summary = {key: report[key] for key in keys if key in report}
    inspection = report.get('inspection', {})
    if isinstance(inspection, dict):
        summary['inspection'] = {key: inspection[key] for key in ('scene', 'workflow_id', 'timeline', 'status', 'manual_changes') if key in inspection and key != 'manual_changes'}
        for key in ('clips', 'unknowns', 'manual_changes'):
            if isinstance(inspection.get(key), (list, dict)):
                summary['inspection'][key + '_count'] = len(inspection[key])
    verification = report.get('verification')
    if isinstance(verification, dict):
        summary['verification'] = {key: verification[key] for key in ('status', 'blender', 'build', 'non_target_protection', 'resources', 'error') if key in verification}
    outputs = report.get('outputs')
    if isinstance(outputs, dict):
        summary['outputs'] = {key: outputs[key] for key in ('status', 'video', 'srt', 'audio', 'verification', 'checks', 'frames_count', 'shots_count') if key in outputs}
    files = report.get('output_files', [])
    summary['output_file_count'] = len(files)
    summary['output_files'] = files[:8]
    for key in ('state', 'health', 'source_sha256', 'source_unchanged'):
        if key in (result.get('data') or {}):
            summary[key] = result['data'][key]
    if result.get('error'):
        summary['status'] = {'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get(result['error']['code'], 'failed')
    if root and (root / 'video-progress.json').is_file():
        summary['progress'] = progress_summary(read_json(root / 'video-progress.json'))
    out.update(output_format='compact', data=summary, artifacts={'job_directory': str(root)} if root else {})
    if root:
        for name in ('request', 'result', 'video-report', 'video-progress', 'video-reopen', 'video-shots', 'video-manifest'):
            path = root / (name + '.json')
            if path.is_file():
                out['artifacts'][name] = descriptor(path)
    return out
