# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, read-only discovery and lossless report-backed output for material agents."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from protocol import ROOT, VERSION, Failure, envelope, read_json

SCHEMAS = ROOT / 'docs/cli/schemas'
OPERATIONS = {
    'run': ('material-run-request.schema.json', 'material-workflow-manifest.schema.json', 'material-workflow-report.schema.json', 'assemble, assign, save/reopen, render views and contact sheet'),
    'batch': ('material-batch-request.schema.json', 'material-batch-manifest.schema.json', 'material-batch-report.schema.json', 'explicit assignments, isolated failures and verified partial resume'),
    'study': ('material-study-request.schema.json', 'material-batch-manifest.schema.json', 'material-study-report.schema.json', 'one owned worker: texture semantic warnings, bounded parameter candidates, fixed views, phase progress and comparison sheets'),
    'template-save': ('material-template-save-request.schema.json', None, 'material-workflow-template-report.schema.json', 'export managed material template and separate bindings'),
    'preview': (None, None, 'preview-report.schema.json', 'existing isolated material preview; not the layered workflow'),
}

def descriptor(path):
    path = Path(path)
    if not path.is_file():
        raise Failure('NOT_FOUND', 'Interface artifact not found: ' + str(path))
    content = path.read_bytes()
    return {'file': str(path.resolve()), 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}


def progress_summary(value):
    """Keep useful progress without replaying checkpoint manifests or face lists."""
    if not isinstance(value, dict): return value
    keys = ('stage', 'status', 'elapsed_seconds', 'planned_assignments', 'completed_assignments',
            'successful_assignments', 'planned_views', 'completed_views', 'successful_views',
            'planned_cases', 'completed_cases', 'current_case', 'current_assignment', 'current_view',
            'latest_checkpoint', 'latest_successful_image', 'completion', 'timings', 'next_action',
            'retry_case_ids', 'error')
    out = {key: deepcopy(value[key]) for key in keys if key in value}
    checkpoint = value.get('checkpoint')
    if isinstance(checkpoint, dict):
        out['checkpoint'] = {key: checkpoint[key] for key in ('file', 'expected_sha256') if key in checkpoint}
    if isinstance(value.get('current_batch'), dict):
        out['current_batch'] = progress_summary(value['current_batch'])
    failures = value.get('failures')
    if isinstance(failures, list):
        out['failures'] = deepcopy(failures[:12])
        out['failures_omitted'] = max(0, len(failures)-12)
    return out

def _section(schema, section):
    if section == 'manifest': return schema
    # v1 and v2 share preview/target. Layers differ and must retain both choices.
    choices = schema.get('oneOf') or schema.get('anyOf') or [schema]
    values = []
    for row in choices:
        properties = row.get('properties', {})
        if 'assignments' in properties and section in ('layers', 'target'):
            properties = properties['assignments']['items']['properties']
        values.append(properties.get(section))
    values = [row for row in values if row is not None]
    unique = list({json.dumps(row, sort_keys=True): row for row in values}.values())
    if not unique: raise Failure('INVALID_REQUEST', 'Section unavailable: ' + section)
    return unique[0] if len(unique) == 1 else {'anyOf': unique}

def describe(operation=None, include_schema=False, section=None):
    if operation is not None and operation not in OPERATIONS:
        raise Failure('INVALID_REQUEST', 'Unknown material operation: ' + operation)
    if (include_schema or section) and operation is None:
        raise Failure('INVALID_REQUEST', '--schema/--section requires --operation')
    if section and include_schema:
        raise Failure('INVALID_REQUEST', 'Choose --schema or --section')
    rows = []
    for name in ([operation] if operation else OPERATIONS):
        request, manifest, report, purpose = OPERATIONS[name]
        row = {'command': 'material.' + name, 'purpose': purpose, 'execution': 'standalone_job',
               'request_schema': descriptor(SCHEMAS / request) if request else None,
               'manifest_schema': descriptor(SCHEMAS / manifest) if manifest else None,
               'report_schema': descriptor(SCHEMAS / report)}
        if name == 'study':
            from material_study_contract import MAX_CASES, MAX_VIEWS, MAX_TOTAL_VIEWS
            row['limits'] = {'cases': MAX_CASES, 'views_per_case': MAX_VIEWS, 'total_views': MAX_TOTAL_VIEWS,
                             'parameter_selection': 'manual', 'white_scope': 'named layer only'}
        if request:
            request_doc = read_json(SCHEMAS / request)
            row['required_params'] = request_doc['properties']['params'].get('required', [])
            row['input_options'] = request_doc['properties']['params'].get('anyOf', [])
            if include_schema: row['schema'] = request_doc
        elif include_schema:
            public = read_json(SCHEMAS / 'request.schema.json')
            row['schema'] = next(r['then']['properties']['params'] for r in public['allOf']
                if r.get('if', {}).get('properties', {}).get('command', {}).get('const') == 'material.preview')
        if section:
            if not manifest: raise Failure('INVALID_REQUEST', 'This operation has no layered manifest')
            row['section'] = section
            if section == 'study':
                if name != 'study': raise Failure('INVALID_REQUEST', 'Study section requires study operation')
                row['schema'] = request_doc['properties']['params']['properties']['study']
            else:
                row['schema'] = _section(read_json(SCHEMAS / manifest), section)
        rows.append(row)
    return envelope('material.describe', data={'interface_version': '1.0', 'operations': rows,
        'compact_result_schema': descriptor(SCHEMAS / 'material-compact-result.schema.json'),
        'usage': 'Global options precede commands. Use --compact request <json>; read report references only when needed. Full result.json remains authoritative.',
        'constraints': ['Saved disk or persistent GUI snapshot; explicit target and source/resource SHA',
            'Existing verified UV; generated spatial PBR layers and complex masks',
            'material workflows are standalone jobs, not pipeline DAG entries']})

def compact(result, requested_command=None):
    """Project a returned envelope, never change the full result/report on disk."""
    command = result.get('command') or requested_command
    artifacts = result.get('artifacts') or {}
    root = Path(artifacts['job_directory']).resolve() if artifacts.get('job_directory') else None
    request = None
    if root and (root / 'request.json').is_file():
        request = read_json(root / 'request.json')
        if command and command.startswith('job.'):
            command = request.get('command')
    if not command or not command.startswith('material.') or command == 'material.describe':
        raise Failure('INVALID_REQUEST', '--compact supports material jobs only')
    out = {key: deepcopy(result.get(key)) for key in ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error')}
    out['output_format'] = 'compact'
    if out['command'] is None: out['command'] = command
    data = result.get('data') or {}
    job_state = {key: deepcopy(data[key]) for key in ('state', 'health', 'cancel_requested') if key in data}
    report_path = None
    if root:
        for name in ('study-report.json', 'batch-report.json', 'workflow-report.json', 'template-report.json'):
            path = root / name
            if path.is_file():
                report_path = path
                # Errors/async status may lack worker data; use the owned report for evidence.
                if 'outputs' not in data and 'assignments' not in data: data = read_json(path)
                break
    summary = {key: deepcopy(data[key]) for key in ('status', 'source', 'source_saved', 'source_unchanged', 'source_sha256', 'seconds', 'checks', 'resume', 'limitations', 'error') if key in data}
    for key in ('progress', 'timings', 'study', 'baseline', 'cases', 'sheets', 'semantic_analysis_ref', 'active_case'):
        if key in data: summary[key] = deepcopy(data[key])
    if 'progress' in summary: summary['progress'] = progress_summary(summary['progress'])
    semantic = data.get('semantic_analysis')
    if isinstance(semantic, dict):
        warnings = semantic.get('warnings', [])
        summary['semantic_analysis'] = {'status': semantic.get('status'), 'image_count': len(semantic.get('images', [])),
            'warning_count': len(warnings), 'warnings': deepcopy(warnings[:12]),
            'warnings_omitted': max(0, len(warnings)-12), 'alignment': 'not_proven',
            'interpretation': 'Numerical review hints, not aesthetic approval or physical calibration'}
    if command == 'material.batch':
        summary['materials'] = [{'id': row['id'], 'status': row['status'], 'reused': row.get('reused'),
            'counts': (row.get('result') or {}).get('counts'),
            'changed_fields': (row.get('result') or {}).get('changed_fields', [])[:64]}
            for row in data.get('assignments', [])]
    summary.update(job_state)
    terminal = {'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get((result.get('error') or {}).get('code'))
    if not terminal and job_state.get('state') in ('cancelled', 'timed_out', 'failed'):
        terminal = job_state['state']
    if terminal:
        summary['persisted_report_status'] = summary.get('status')
        summary['status'] = terminal
    if isinstance((result.get('data') or {}).get('progress'), dict):
        summary['progress'] = progress_summary(result['data']['progress'])
    if request:
        p = request.get('params', {})
        summary.setdefault('source', {key: p[key] for key in ('file', 'expected_sha256') if key in p})
    for name in ('candidate', 'sheet'):
        if name not in data: continue
        if name == 'candidate' and data[name]:
            summary[name] = {'file': data[name], 'sha256': data.get('candidate_sha256')}
        else:
            summary[name] = {k: v for k, v in data[name].items() if k in ('file', 'sha256', 'bytes', 'width', 'height', 'status')} if isinstance(data[name], dict) else data[name]
    summary['counts'] = {key: dict(Counter(row.get('status', 'unknown') for row in data.get(key, []))) for key in ('assignments', 'outputs')}
    summary['failures'] = {key: [{k: v for k, v in row.items() if k in ('id', 'status', 'code', 'error', 'objects')} for row in data.get(key, []) if row.get('status') != 'pass'] for key in ('assignments', 'outputs')}
    if command == 'material.study':
        cases = data.get('cases', [])
        outputs = [output for case in cases for output in case.get('outputs', [])]
        summary['counts']['cases'] = dict(Counter(case.get('status', 'unknown') for case in cases))
        summary['counts']['outputs'] = dict(Counter(output.get('status', 'unknown') for output in outputs))
        summary['failures']['cases'] = [{k: v for k, v in case.items() if k in ('id', 'status', 'error')}
                                      for case in cases if case.get('status') != 'pass']
        summary['failures']['outputs'] = [{'case': case['id'], **{k: v for k, v in output.items() if k in ('id', 'status', 'code', 'error')}}
                                         for case in cases for output in case.get('outputs', []) if output.get('status') != 'pass']
    # Preview/template artifacts live outside the layered report shape.
    for key in ('template', 'bindings', 'template_sha256', 'bindings_sha256', 'output'):
        if key in data: summary[key] = deepcopy(data[key])
    out['data'] = summary
    out['artifacts'] = {'job_directory': str(root)} if root else {}
    if root:
        for name in ('request.json', 'result.json'):
            path = root / name
            if path.is_file(): out['artifacts'][name.split('.')[0]] = descriptor(path)
    if report_path: out['artifacts']['report'] = descriptor(report_path)
    return out
