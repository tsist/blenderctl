"""Cheap geometry contract discovery: no Blender or job is started."""
import hashlib
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from geometry_nodes_workflow_addon import contract
from protocol import VERSION, Failure


def describe(operation=None, schema=False, section=None):
    schemas = {'run': contract.RUN_PARAMS_SCHEMA, 'inspect': contract.INSPECT_PARAMS_SCHEMA,
               'manifest': contract.MANIFEST_SCHEMA, 'report': contract.REPORT_SCHEMA}
    if section:
        if section not in contract.MANIFEST_SCHEMA.get('properties', {}):
            raise Failure('INVALID_REQUEST', 'Unknown geometry manifest section: ' + section)
        return {'operation': 'manifest', 'section': section,
                'schema': contract.MANIFEST_SCHEMA['properties'][section]}
    if schema:
        if operation not in schemas:
            raise Failure('INVALID_REQUEST', '--schema requires run, inspect, manifest or report')
        return {'operation': operation, 'schema': schemas[operation]}
    source = Path(contract.__file__)
    return {'cli_version': VERSION, 'plugin_version': '0.2.0',
            'geometry_schema_version': '1.1',
            'operations': ['geometry.run', 'geometry.inspect', 'geometry.session'],
            'contract': {'file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()},
            'recipes': ['spiral_stair', 'curve_fence', 'surface_scatter'],
            'state': 'SAVED_DISK_V1 for worker; explicit owned live-session capture/apply for GUI',
            'ownership': 'Stable user wrapper, replaceable managed core; isolated targets by default',
            'verification': 'Independent Blender reopen; temporal runs also compare scrambled baked-frame witnesses',
            'scope': {'included': ['native graph mechanisms', 'static geometry', 'Repeat', 'managed Simulation',
                                   'managed Bake', 'immutable DISK cache receipts', 'same-project targets'],
                      'later': ['unmanaged temporal graph adoption', 'motion-blur subframe cache', 'cross-file batching']},
            'compact': '--compact request <json> and job status/result/wait for geometry.run/inspect; full disk reports remain authoritative',
            'compact_result_schema': descriptor(Path(contract.__file__).resolve().parents[2] / 'docs/cli/schemas/geometry-compact-result.schema.json'),
            'note': 'Discovered native nodes do not certify every effect. Read per-target and verification status.'}


from collections import Counter
from copy import deepcopy
from pathlib import Path
from material_interface import descriptor
from protocol import Failure, read_json


def pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def progress_summary(value):
    result = pick(value, ('status', 'completed', 'total', 'current_target', 'checkpoint',
                         'recovery', 'error', 'temporal_error', 'report'))
    temporal = value.get('temporal')
    if isinstance(temporal, dict):
        result['temporal'] = pick(temporal, ('stage', 'frame', 'completed', 'total',
            'target', 'node', 'kind', 'bake', 'bake_id', 'error', 'elapsed_seconds'))
    return result


def compact(result, requested_command=None):
    command = requested_command or result.get('command')
    root = (result.get('artifacts') or {}).get('job_directory')
    root = Path(root).resolve() if root else None
    request = read_json(root / 'request.json') if root and (root / 'request.json').is_file() else None
    if request:
        command = request['command']
    if command not in ('geometry.run', 'geometry.inspect'):
        raise Failure('INVALID_REQUEST', '--compact supports geometry run/inspect jobs')
    out = pick(result, ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error'))
    out['output_format'] = 'compact'
    if not out.get('command'): out['command'] = command
    original_data = result.get('data') or {}
    data = original_data
    report_path = root / 'geometry-report.json' if root else None
    if report_path and report_path.is_file():
        data = read_json(report_path)
    summary = pick(data, ('status', 'target_outcome', 'source', 'source_sha256',
        'source_unchanged', 'implementation_sha256', 'checks', 'finalization',
        'metrics', 'recovery', 'error', 'verification', 'cache_manifest'))
    summary.update(pick(original_data, ('source_sha256', 'source_unchanged', 'cache_manifest')))
    summary['operation'] = command
    summary.update(pick(original_data, ('discovered_count',)))
    summary.update(pick(data, ('cache_validation',)))
    if isinstance(data.get('cache_replay'), dict):
        summary['cache_replay'] = pick(data['cache_replay'], ('status', 'frames', 'order', 'mismatch', 'error'))
    if request:
        summary.setdefault('source', pick(request.get('params', {}), ('file', 'expected_sha256')))
    candidate = data.get('candidate') or original_data.get('candidate')
    if candidate:
        summary['candidate'] = candidate if isinstance(candidate, dict) else {
            'file': candidate, 'sha256': data.get('candidate_sha256') or original_data.get('candidate_sha256')}
    targets = data.get('targets', [])
    summary['targets'] = [pick(row, ('id', 'object', 'modifier', 'status', 'rolled_back',
        'error', 'core_sha256', 'parameters')) for row in targets]
    summary['counts'] = {'targets': dict(Counter(row.get('status', 'unknown') for row in targets)),
                         'previews': len(data.get('previews', []))}
    temporal = data.get('temporal')
    if isinstance(temporal, dict):
        summary['temporal'] = pick(temporal, ('status', 'stage', 'fps', 'frame_start',
            'frame_end', 'observe_frames', 'metrics', 'cache_manifest', 'error'))
        summary['temporal']['bakes'] = [pick(row, ('target', 'node_id', 'group_path', 'kind', 'bake_id',
            'operator_result', 'frame_coverage', 'status', 'error')) for row in temporal.get('bakes', [])]
    if root and (root / 'geometry-progress.json').is_file():
        summary['progress'] = progress_summary(read_json(root / 'geometry-progress.json'))
    summary.update(pick(original_data, ('state', 'health', 'cancel_requested')))
    terminal = {'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get((result.get('error') or {}).get('code'))
    if not terminal and original_data.get('state') in ('cancelled', 'timed_out', 'failed'):
        terminal = original_data['state']
    if terminal:
        summary['persisted_report_status'] = summary.get('status')
        summary['status'] = terminal
    out['data'] = summary
    out['artifacts'] = {'job_directory': str(root)} if root else {}
    if root:
        for name, filename in (('request', 'request'), ('result', 'result'),
                              ('report', 'geometry-report'), ('progress', 'geometry-progress'),
                              ('temporal_report', 'geometry-temporal-report'), ('cache_manifest', 'geometry-cache-manifest')):
            path = root / (filename + '.json')
            if path.is_file():
                out['artifacts'][name] = descriptor(path)
    return out
