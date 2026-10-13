"""Small evidence projections and deterministic delivery indexes; full files win."""
from copy import deepcopy
import os
from pathlib import Path
from protocol import Failure, atomic_json, digest, read_json


def descriptor(path, checkpoint=None):
    path = Path(path)
    return {'file': str(path.resolve()), 'expected_sha256': digest(path,checkpoint),
            'bytes': path.stat().st_size}


FIELDS = ('status', 'state', 'health', 'error', 'reason', 'limitations',
          'source_saved', 'source_unchanged', 'source_sha256', 'source_sha256_before',
          'candidate_sha256', 'runtime_executed', 'seconds', 'elapsed_seconds',
          'cancel_requested', 'closed_inputs', 'reuse', 'timing', 'checks')


def projection(data):
    if not isinstance(data, dict):
        return {'value_type': type(data).__name__}
    out = {k: deepcopy(data[k]) for k in FIELDS if k in data}
    failures=[]
    def walk(value, path=''):
        if isinstance(value, dict):
            status=str(value.get('status',value.get('state',''))).casefold()
            if value.get('error') or status in ('fail','failed','cancelled','timed_out','blocked','not_run'):
                failures.append({'path':path or '/', **{k:deepcopy(value[k]) for k in
                                ('status','state','error','reason') if k in value}})
            for key, child in value.items():
                if key != 'error':walk(child,path+'/'+str(key))
        elif isinstance(value,list):
            for index, child in enumerate(value):walk(child,path+'/'+str(index))
    walk(data)
    out['failure_count']=len(failures);out['failure_witnesses']=failures
    for key in ('steps', 'targets', 'assignments', 'outputs', 'checked', 'failed', 'deferred'):
        values = data.get(key)
        if not isinstance(values, list):
            continue
        out[key + '_count'] = len(values)
        if key == 'outputs':
            continue
        # Bounded contracts cap steps/targets; the summary retains every failure
        # and all rows' identity/state rather than hiding late errors behind a cap.
        out[key] = [{k: deepcopy(row[k]) for k in
                    ('id', 'index', 'command', 'object', 'modifier', 'phase',
                     'state', 'status', 'error', 'reason', 'closed_inputs') if k in row}
                    for row in values if isinstance(row, dict)]
    return out


def compact(result, requested_command=None):
    """Keep envelope errors, protection outcomes, and hashes of full evidence."""
    out = {k: deepcopy(result.get(k)) for k in
           ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error')}
    if not out['command']:
        out['command'] = requested_command
    out['output_format'] = 'compact'
    out['data'] = projection(result.get('data') or {})
    artifacts = result.get('artifacts') or {}
    root = Path(artifacts['job_directory']).resolve() if artifacts.get('job_directory') else None
    reports = {}
    if root:
        for path in sorted(root.glob('*.json')):
            if path.name in ('status.json', 'request.json', 'result.json', 'launch.json') or 'report' in path.name or path.name in ('timing.json', 'render-timing.json', 'workflow-preflight.json', 'workflow-delivery.json'):
                reports[path.name] = descriptor(path)
        # Failed pipeline envelopes intentionally have no data. Recover only
        # the bounded projection from the authoritative partial report.
        for name in ('pipeline-report.json', 'workflow-preflight.json', 'render-report.json'):
            if name in reports:
                out['data']['report_summary'] = projection(read_json(reports[name]['file']))
                break
    out['artifacts'] = {'job_directory': str(root) if root else None,
                        'reports': reports, 'artifact_count': len(artifacts)}
    out['read_scope'] = 'Full result/report remains authoritative; use SHA-bound references for omitted domain details.'
    return out


def delivery_index(job, result, checkpoint=None):
    """An owned-job index, not a claim that external dependencies are packaged."""
    job = Path(job).resolve()
    rows = []
    expected={}
    for step in (result.get('data') or {}).get('steps',[]):
        expected.update(step.get('output_hashes') or {})
    paths = []
    for directory, dirs, files in os.walk(job, followlinks=False):
        if checkpoint:checkpoint()
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise Failure('UNSUPPORTED', 'Delivery index cannot follow directory links')
        paths.extend(Path(directory) / name for name in files)
    for path in sorted(paths):
        if checkpoint:checkpoint()
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise Failure('UNSUPPORTED', 'Delivery index cannot follow filesystem links')
        if not path.is_file() or path.name in ('status.json', 'cancel.request', 'worker.gate', 'workflow-delivery.json', 'DELIVERY.md') or path == job / 'result.json':
            continue
        desc=descriptor(path,checkpoint)
        if str(path) in expected and desc['expected_sha256']!=expected[str(path)]:
            raise Failure('CONFLICT','Accepted worker output changed during delivery finalization: '+str(path))
        rows.append({'path': str(path.relative_to(job)).replace('\\', '/'), **desc})
    report = {'delivery_index_version': '1.0', 'job_id': job.name,
              'command': result.get('command'), 'ok': result.get('ok'),
              'error': result.get('error'), 'evidence': projection(result.get('data') or {}),
              'files': rows, 'file_count': len(rows),
              'bytes': sum(row['bytes'] for row in rows),
              'scope': 'Owned job files only; external dependencies and caches require explicit project.package and independent reopen.',
              'acceptance': 'Execution evidence only; visual/user approval is recorded separately.'}
    if checkpoint:checkpoint()
    atomic_json(job / 'workflow-delivery.json', report)
    lines = [f"# {result.get('command')} / {job.name}", '',
             f"Execution: {'passed' if result.get('ok') else 'failed'}", '', report['scope'], '',
             f"Files: {len(rows)}; bytes: {report['bytes']}", '',
             'Authoritative index: workflow-delivery.json (file paths and SHA-256).', '',
             'Visual acceptance: not inferred from execution success.', '']
    if result.get('error'):
        lines.extend([str(result['error']), ''])
    (job / 'DELIVERY.md').write_text('\n'.join(lines), encoding='utf-8')
    return descriptor(job / 'workflow-delivery.json')
