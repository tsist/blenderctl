"""No-bpy Toon discovery and terminal-aware report projection."""
from copy import deepcopy
from pathlib import Path
import hashlib
import sys

from toon_identity import ROOT, implementation_sha256
sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
sys.path.insert(0, str(ROOT / 'tools'))
from protocol import COMMANDS, VERSION, Failure, envelope, read_json
from toon_workflow_addon import contract, __version__


def descriptor(path):
    path = Path(path)
    content = path.read_bytes()
    return {'file': str(path.resolve()), 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}


def describe(operation=None, schema=False, section=None):
    try:
        if operation and operation.startswith('template'):
            if section:raise contract.WorkflowError('INVALID_REQUEST','Template describe has no manifest section')
            from toon_template_contract import describe as template_describe
            data=template_describe(operation,schema)
        else:
            data = contract.describe(operation, schema, section)
            if operation is None:
                from toon_template_contract import describe as template_describe
                data['operations']+=template_describe()['operations']
    except contract.WorkflowError as exc:
        raise Failure(exc.code, exc.code + ' at ' + exc.path + ': ' + str(exc)) from exc
    registered = {'toon.run', 'toon.inspect', 'toon.template-save', 'toon.template-edit', 'toon.template-apply'}.issubset(COMMANDS)
    data.update(interface_version=__version__, cli_version=VERSION,
                implementation_sha256=implementation_sha256(),
                registration_status='registered' if registered else 'candidate_not_registered',
                production_execution_available=registered,
                deployment_identity='Recompute implementation pin after copying staged modules to their formal paths',
                usage='Global options precede commands. Full disk results remain authoritative; no job is created by describe.')
    for row in data['operations']:
        row['params_schema_descriptor'] = descriptor(ROOT / 'docs/cli/schemas' / (row['command'] + '.schema.json'))
    return envelope('toon.describe', data=data)


def _pick(value, names):
    return {key: deepcopy(value[key]) for key in names if key in value}


def _owned(root, filename):
    path = root / filename
    if not path.is_file() or path.is_symlink():
        return None
    try: path.resolve().relative_to(root.resolve())
    except ValueError: raise Failure('CONFLICT', 'Compact report escaped owned job directory')
    return path


def compact(result, requested_command=None):
    """Optional display only; never changes stored result, report or exit status."""
    root = (result.get('artifacts') or {}).get('job_directory')
    root = Path(root).resolve() if root else None
    command = requested_command or result.get('command')
    request = None
    if root:
        path = _owned(root, 'request.json')
        request = read_json(path) if path else None
        if request: command = request.get('command')
    if command not in ('toon.run','toon.inspect','toon.template-save','toon.template-edit','toon.template-apply'):
        raise Failure('INVALID_REQUEST', '--compact supports Toon run/inspect jobs only')
    out = _pick(result, ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error'))
    out.update(command=command, output_format='compact')
    original = result.get('data') or {}
    report = None
    if root:
        report_name = ('toon-template-report.json' if command in
                       ('toon.template-save','toon.template-edit','toon.template-apply') else 'toon-report.json')
        path = _owned(root, report_name)
        if path: report = read_json(path)
    data = report if isinstance(report, dict) else original
    summary = _pick(data, ('status', 'operation', 'source', 'implementation_sha256', 'scope_id',
        'candidate', 'candidate_sha256', 'checks', 'verification', 'finalization', 'metrics',
        'qualification', 'limitations', 'unsupported', 'error', 'recovery', 'counts'))
    if isinstance(data.get('resources'), list): summary['resource_count'] = len(data['resources'])
    for key in ('targets', 'objects'):
        if isinstance(data.get(key), list):
            rows = data[key]
            summary[key] = [_pick(row, ('object_name', 'name', 'object_id', 'mesh_id', 'status', 'error'))
                            for row in rows[:16] if isinstance(row, dict)]
            summary[key + '_count'] = len(rows)
            summary[key + '_omitted'] = max(0, len(rows) - 16)
    summary.update(_pick(original, ('source_sha256', 'source_unchanged', 'artifact_accepted', 'application_status', 'output', 'state', 'health', 'cancel_requested',
                                    'candidate_accepted', 'parent_output_validation')))
    if request:
        summary.setdefault('source', _pick(request.get('params', {}), ('file', 'expected_sha256')))
        summary.setdefault('implementation_sha256', request.get('params', {}).get('implementation_sha256'))
    if root:
        path = _owned(root, 'toon-progress.json')
        if path:
            progress = read_json(path)
            summary['progress'] = _pick(progress, ('stage', 'status', 'completed_frames', 'total_frames',
                'frame', 'current_frame', 'elapsed_seconds', 'latest_checkpoint', 'recovery', 'error'))
    terminal = {'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get((result.get('error') or {}).get('code'))
    if not terminal and original.get('state') in ('cancelled', 'timed_out', 'failed'):
        terminal = original['state']
    if not terminal and not result.get('ok'): terminal = 'failed'
    if terminal:
        summary['persisted_report_status'] = summary.get('status')
        summary['status'] = terminal
        summary['candidate_accepted'] = False
    elif original.get('state') in ('queued', 'running'):
        summary['persisted_report_status'] = summary.get('status')
        summary['status'] = original['state']
        summary['candidate_accepted'] = False
    out['data'] = summary
    out['artifacts'] = {'job_directory': str(root)} if root else {}
    if root:
        for filename in ('request.json', 'result.json', 'toon-report.json', 'toon-progress.json',
                         'toon-template-report.json', 'toon-template.json', 'toon-resolved-request.json',
                         'toon-state.json', 'toon-reopen.json', 'toon-manifest.json', 'toon-resources.json'):
            path = _owned(root, filename)
            if path: out['artifacts'][filename] = descriptor(path)
    return out
