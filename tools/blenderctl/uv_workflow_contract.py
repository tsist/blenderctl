"""Host-safe UV request contract, implementation identity and discovery.

This module must stay importable without Blender. The native core is loaded
only by the worker adapter and by the independent verifier process.
"""
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import CODES, Failure, VERSION, digest, read_json
from uv_workflow_addon import contract as addon_contract


MODULE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = MODULE_DIR.parent
ROOT = TOOLS_DIR.parent
ADDON_DIR = TOOLS_DIR / 'uv_workflow_addon'

# Hashed runtime surface: the adapter entry points plus the complete production
# Python surface of the UV add-on. Never hash the whole (very large) tools tree.
CLI_RUNTIME_FILES = ('cli.py', 'protocol.py', 'runner.py', 'worker.py',
                     'uv_workflow.py', 'uv_workflow_contract.py', 'uv_verifier.py', 'uv_resources.py', 'uv_saved_protection.py',
                     'uv_batch.py', 'pipeline.py', 'pipeline_contract.py',
                     'filesystem.py', 'geometry_workflow.py', 'lighting_workflow.py',
                     'execution_contract.py', 'uv_grid_transfer.py', 'uv_grid_transfer_contract.py')
RUN_OPERATIONS = ('generate', 'validate', 'adopt', 'restore', 'edit', 'recipe', 'settings', 'seams', 'lightmap', 'copy_uv', 'transfer', *addon_contract.UDIM_AUTHORING)
ALL_OPERATIONS = (*RUN_OPERATIONS, 'inspect')
PARAMS_REQUIRED = ('file', 'expected_sha256', 'resources', 'manifest')
PARAMS_OPTIONAL = ('implementation_sha256',)
RESOURCE_LIMIT = 256


def _fail(message, code='INVALID_REQUEST'):
    raise Failure(code, message)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except addon_contract.WorkflowError as exc:
        raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST', str(exc)) from exc


def _file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        _fail(label + ' must be lowercase SHA-256')
    return value


def _absolute(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        _fail(label + ' must be a non-empty path')
    path = Path(value)
    if not path.is_absolute():
        _fail(label + ' must be an absolute path')
    return str(path.resolve())


def _descriptor(value, label, *, blend=False):
    if not isinstance(value, dict) or set(value) != {'file', 'expected_sha256'}:
        _fail(label + ' requires exactly file and expected_sha256')
    result = {'file': _absolute(value['file'], label + '.file'),
              'expected_sha256': _sha(value['expected_sha256'], label + '.expected_sha256')}
    if blend and Path(result['file']).suffix.lower() != '.blend':
        _fail(label + ' must be a .blend file')
    return result


def _relative_runtime_path(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace('\\', '/')
    except ValueError:
        return str(Path(path).resolve()).replace('\\', '/')


def runtime_files():
    """Explicit bounded file list used by implementation identity."""
    return ([MODULE_DIR / name for name in CLI_RUNTIME_FILES]
            + sorted(ADDON_DIR.glob('*.py'))
            + [TOOLS_DIR/'geometry_nodes_workflow_addon'/name for name in ('__init__.py','core.py','contract.py','inspection.py')])


def runtime_documents():
    """Exact immutable production files used by UV requests and verification."""
    documents = []
    for path in runtime_files():
        if not path.is_file():
            raise Failure('NOT_FOUND', 'UV runtime document missing: ' + str(path))
        documents.append({'file': str(path.resolve()),
                          'expected_sha256': _file_sha256(path)})
    return documents


def _implementation_from_documents(documents):
    rows = [{**row, 'file': _relative_runtime_path(row['file'])}
            for row in documents]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def implementation_sha256():
    return _implementation_from_documents(runtime_documents())


def _identity(value):
    if not isinstance(value, dict):
        _fail('params must be an object')
    result = copy.deepcopy(value)
    supplied = result.pop('implementation_sha256', None)
    current = implementation_sha256()
    if supplied is not None:
        _sha(supplied, 'implementation_sha256')
        if supplied != current:
            _fail('UV implementation changed since request normalization', 'CONFLICT')
    return result, current


def normalize_params(value, command):
    """Normalize one uv.run or uv.inspect params object.

    Unknown top-level or descriptor fields are rejected. The manifest is
    normalized by the add-on's host-safe contract before operation routing.
    """
    if command == 'uv.grid-transfer':
        from uv_grid_transfer_contract import normalize
        return normalize(value)
    if command not in ('uv.run', 'uv.inspect'):
        _fail('Unknown UV command')
    result, current = _identity(value)
    unknown = set(result) - set(PARAMS_REQUIRED)
    if unknown:
        _fail('Unknown UV params fields: ' + ', '.join(sorted(unknown)))
    missing = set(PARAMS_REQUIRED) - set(result)
    if missing:
        _fail('UV params require file, expected_sha256, resources and manifest; missing: '
              + ', '.join(sorted(missing)))
    source = _descriptor({'file': result['file'],
                          'expected_sha256': result['expected_sha256']},
                         'source', blend=True)
    resources = result['resources']
    if not isinstance(resources, list) or len(resources) > RESOURCE_LIMIT:
        _fail('resources must be an array of 0..' + str(RESOURCE_LIMIT) + ' descriptors')
    normalized_resources = []
    seen = set()
    for index, row in enumerate(resources):
        item = _descriptor(row, 'resources[' + str(index) + ']')
        key = os.path.normcase(item['file'])
        if key in seen:
            _fail('Duplicate UV resource path: ' + item['file'])
        seen.add(key)
        normalized_resources.append(item)
    manifest = _call(addon_contract.normalize, result['manifest'])
    operation = manifest['operation']
    if command == 'uv.inspect' and operation != 'inspect':
        _fail('uv.inspect requires manifest.operation=inspect')
    if command == 'uv.run' and operation not in RUN_OPERATIONS:
        _fail('uv.run requires manifest.operation in ' + '/'.join(RUN_OPERATIONS))
    return {**source, 'resources': normalized_resources, 'manifest': manifest,
            'implementation_sha256': current}


def normalize_inspect_params(value):
    return normalize_params(value, 'uv.inspect')


def udim_report_fields(layout):
    """Keep the frozen C1 declaration compatible with strict consumers."""
    declaration = ('enabled', 'tiles', 'tile_resolution', 'per_object_tiles')
    return {'udim': {key: layout[key] for key in declaration},
            'udim_measurements': {key: value for key, value in layout.items()
                                  if key not in declaration}}


def signature(params):
    body = {key: params.get(key) for key in
            ('file', 'expected_sha256', 'resources', 'manifest', 'implementation_sha256')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def input_documents(params, command=None):
    """Source, declared resources and the bounded UV runtime identity."""
    if command is not None and command not in ('uv.run', 'uv.inspect', 'uv.grid-transfer'):
        _fail('Unknown UV command')
    rows = []
    if 'file' in params:
        rows.append({'file': params['file'], 'expected_sha256': params['expected_sha256']})
    rows.extend(params.get('resources', []))
    rows.extend(runtime_documents())
    merged = {}
    for row in rows:
        key = os.path.normcase(str(Path(row['file']).resolve()))
        if key in merged and merged[key]['expected_sha256'] != row['expected_sha256']:
            _fail('Conflicting UV input hashes: ' + row['file'], 'CONFLICT')
        merged[key] = {'file': row['file'], 'expected_sha256': row['expected_sha256']}
    return list(merged.values())


def descriptor(path):
    path = Path(path)
    return {'file': str(path.resolve()), 'sha256': digest(path)}


def describe():
    contract = addon_contract.describe()
    runtime = runtime_documents()
    return {
        'cli_version': VERSION,
        'uv_schema_version': '1.0',
        'plugin_version': contract.get('version'),
        'operations': ['uv.run', 'uv.inspect', 'uv.batch', 'uv.batch-resume', 'uv.grid-transfer'],
        'grid_transfer': {
            'params': ['file', 'expected_sha256', 'destination', 'manifest'],
            'optional': ['mask_archive', 'implementation_sha256'],
            'scope': 'Saved mesh pairs, packed Non-Color CHANNEL_PACKED RGBA8 tangent normal, base-level bilinear, integer translation and exact geometry/TBN checks',
            'outputs': ['grid-transfer/normal.png', 'grid-transfer/receipt.json'],
            'publication': 'Only after continuous footprint and PNG decode checks; original files are guarded, no BLEND changes',
        },
        'batch': {'version': '1.0', 'max_steps': 32, 'max_workers': 1,
                  'manifest': 'uv_batch_version, policy, steps; existing pipeline budgets/bindings',
                  'commands': ['uv.run', 'uv.inspect'],
                  'recovery': 'batch-resume uses original manifest; batch with reuse_job accepts explicit changed requests',
                  'quality': 'No numeric-pass candidate isolates the step; generate may return several alternatives and only usable_candidates can continue. Every failed/unknown sibling remains reported. Numeric pass still needs visual/purpose review'},
        'manifest_operations': list(ALL_OPERATIONS),
        'run_manifest_operations': list(RUN_OPERATIONS),
        'inspect_manifest_operation': 'inspect',
        'params': {
            'required': list(PARAMS_REQUIRED),
            'optional': list(PARAMS_OPTIONAL),
            'source': 'absolute .blend path plus expected_sha256',
            'resources': 'required array of {file, expected_sha256}; may be empty',
            'manifest': 'normalized by uv_workflow_addon.contract.normalize()',
            'unknown_fields': 'rejected',
        },
        'contract': contract,
        'contract_file': descriptor(Path(addon_contract.__file__).resolve()),
        'implementation_sha256': _implementation_from_documents(runtime),
        'runtime_documents': [{'file': _relative_runtime_path(row['file']),
                               'sha256': row['expected_sha256']} for row in runtime],
        'state_source': 'SAVED_DISK_V1; uv.run saves a UV candidate, grid-transfer writes a new PNG set',
        'candidate': 'New job artifacts only; source and declared resources are deny-write guarded',
        'verification': 'Independent uv_verifier.py reopen is required; describe never launches Blender',
        'quality': 'Candidate hard checks never imply visual or purpose-stretch quality acceptance',
        'not_yet_supported': list(contract.get('not_yet_supported', [])) +
                             ['Undeclared image tile members, UVTILE, image sequences and movies in the public UV adapter'],
    }


def schemas():
    file_schema = {'type': 'object',
                   'properties': {'file': {'type': 'string'},
                                  'expected_sha256': {'type': 'string'}},
                   'required': ['file', 'expected_sha256'],
                   'additionalProperties': False}
    base = {'type': 'object',
            'properties': {'file': {'type': 'string'},
                           'expected_sha256': {'type': 'string'},
                           'resources': {'type': 'array', 'items': file_schema,
                                         'maxItems': RESOURCE_LIMIT},
                           'manifest': {'type': 'object'},
                           'implementation_sha256': {'type': 'string'}},
            'required': list(PARAMS_REQUIRED),
            'additionalProperties': False}
    from uv_batch import MANIFEST
    return {'run': copy.deepcopy(base), 'inspect': copy.deepcopy(base),
            'batch': {'type':'object','properties':{'manifest':copy.deepcopy(MANIFEST),'reuse_job':{'type':'string','pattern':'^[0-9a-f]{32}$'}},'required':['manifest'],'additionalProperties':False},
            'batch-resume': {'type':'object','properties':{'job_id':{'type':'string','pattern':'^[0-9a-f]{32}$'}},'required':['job_id'],'additionalProperties':False}}


def compact(result, requested_command=None):
    """Bounded projection; the complete uv-report.json stays on disk."""
    artifacts = result.get('artifacts') if isinstance(result, dict) else None
    root = None
    if isinstance(artifacts, dict) and artifacts.get('job_directory'):
        root = Path(artifacts['job_directory'])
    command = requested_command or (result.get('command') if isinstance(result, dict) else None)
    if root is not None and (root / 'request.json').is_file():
        command = read_json(root / 'request.json').get('command')
    if command in ('uv.batch','uv.batch-resume'):
        from uv_batch import compact as compact_batch
        return compact_batch(result)
    if not isinstance(command, str) or not command.startswith('uv.'):
        _fail('Not a UV job')
    report = {}
    if root is not None and (root / 'uv-report.json').is_file():
        report = read_json(root / 'uv-report.json')
    elif isinstance(result, dict) and isinstance(result.get('data'), dict):
        report = result['data']
    summary = {key: report[key] for key in
               ('status', 'operation', 'source', 'candidate', 'implementation_sha256',
                'runtime', 'verification', 'resources', 'checks', 'quality_acceptance',
                'non_target_sha256', 'non_target_check', 'render_bindings',
                'lightmap', 'bounds', 'active_render_preserved', 'overwrote_existing',
                'layer', 'target_scope', 'seam_action', 'objects',
                'metrics', 'error', 'support')
               if key in report}
    if isinstance(report.get('seam_suggestions'), dict):
        sug = report['seam_suggestions']
        summary['seam_suggestions'] = {
            k: sug[k] for k in ('angle_degrees', 'include_boundaries', 'max_examples', 'basis', 'note') if k in sug
        }
        if isinstance(sug.get('objects'), dict):
            summary['seam_suggestions']['objects'] = {
                name: {k: row[k] for k in ('edge_count', 'counts', 'examples', 'examples_complete') if k in row}
                for name, row in sug['objects'].items() if isinstance(row, dict)
            }
    if isinstance(report.get('surface'),dict):
        surface=report['surface']
        summary['surface']={key:surface[key] for key in ('status','sample_count','usage','quality_acceptance','limits','evaluation_scope','configured_engine','render_capture_engine','configured_renderer_equivalence','controls_restored') if key in surface}
        summary['surface']['samples']=[{k:s[k] for k in ('frame','mode','status','complete','reason','message','observed_triangles_at_stop','requested_limit','reuse_used','candidate') if k in s} for s in surface.get('samples',[])]
        for compact_sample,sample in zip(summary['surface']['samples'],surface.get('samples',[])):
            q=sample.get('metrics',{})
            compact_sample.update({k:q[k] for k in ('total_triangles','measurement_surface','evaluation_mode') if k in q})
            exact=sample.get('exact_identity')
            if isinstance(exact,dict):
                rows=exact.get('objects',[])
                compact_sample['exact_identity']={k:exact[k] for k in ('comparison','first_mismatch') if k in exact}
                compact_sample['exact_identity'].update(targets=len(rows),matched_targets=sum(r.get('match') is True for r in rows))
    if isinstance(report.get('diagnostics'),dict):
        diagnostic=report['diagnostics']
        summary['diagnostics']={k:diagnostic[k] for k in ('status','basis','mutation','max_examples_per_issue')}
        summary['diagnostics']['objects']=[{'object':row['object'],'geometry':row['geometry'],
            'status':row['status'],'counts':row['counts'],'issues':{k:{f:r[f] for f in ('severity','count','examples_complete')}
            for k,r in row['issues'].items()}} for row in diagnostic['objects']]
    # Job lifecycle lives in the runner envelope, including before a UV report
    # exists. Preserve it independently of the domain report's quality status.
    data = result.get('data') if isinstance(result, dict) else None
    if isinstance(data, dict):
        for key in ('state', 'health', 'cancel_requested', 'progress_error'):
            if key in data:
                summary[key] = data[key]
        state = data.get('state')
        if state in ('cancelled', 'failed', 'timed_out'):
            summary['status'] = state
        if isinstance(data.get('progress'), dict):
            progress_key = ('last_progress' if state and state not in ('queued', 'running')
                            else 'progress')
            summary[progress_key] = {key: data['progress'][key] for key in
                                     ('stage', 'status', 'elapsed_seconds',
                                      'candidate', 'object', 'method', 'frame', 'mode', 'triangles')
                                     if key in data['progress']}
    record = report.get('record')
    if isinstance(record, dict):
        summary['record'] = {key: record[key] for key in
                             ('candidate', 'status', 'method', 'layer', 'revision')
                             if key in record}
        metrics = record.get('metrics', {})
        summary['record']['metrics'] = {key: metrics[key] for key in
                                        ('status', 'quality_acceptance', 'visual',
                                         'purpose_stretch_threshold', 'purpose_checks') if key in metrics}
        if isinstance(record.get('last_edit'), dict):
            summary['record']['last_action'] = record['last_edit'].get('action')
    candidates = report.get('candidates')
    if isinstance(candidates, list):
        summary['candidates'] = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            item = {key: row[key] for key in ('candidate', 'status', 'method', 'layer')
                    if key in row}
            metrics = row.get('metrics')
            if isinstance(metrics, dict):
                item['metrics'] = {key: metrics[key] for key in
                                   ('status', 'quality_acceptance', 'visual',
                                    'purpose_stretch_threshold')
                                   if key in metrics}
            summary['candidates'].append(item)
    if isinstance(summary.get('verification'), dict):
        summary['verification'] = {key: summary['verification'][key] for key in
                                   ('status', 'blender', 'build', 'state', 'target', 'transfer_sources',
                                    'non_target', 'non_target_protection', 'error')
                                   if key in summary['verification']}
    if isinstance(summary.get('resources'), dict):
        summary['resources'] = {key: summary['resources'][key] for key in
                                ('status', 'support', 'limitations', 'packed_policy',
                                 'weak_reference_policy', 'rows')
                                if key in summary['resources']}
        rows = summary['resources'].get('rows')
        if isinstance(rows, list):
            summary['resources']['rows'] = len(rows)
    out = {key: result[key] for key in
           ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error')
           if key in result}
    if result.get('error'):
        summary['status'] = {'CANCELLED': 'cancelled',
                             'TIMEOUT': 'timed_out'}.get(result['error'].get('code'), 'failed')
    if command == 'uv.grid-transfer':
        footprint = report.get('footprint', {})
        summary['footprint'] = {key: footprint[key] for key in ('status', 'totals') if key in footprint}
        if footprint.get('errors'):
            summary['footprint']['first_error'] = footprint['errors'][0]
        grid_accepted = result.get('ok') is True and summary.get('status') == 'texture_written'
        if not grid_accepted:
            summary['candidate'] = None
    out.update(output_format='compact', data=summary,
               artifacts={'job_directory': str(root)} if root is not None else {})
    if root is not None:
        for name in ('request', 'status', 'result', 'uv-report', 'uv-progress', 'uv-reopen',
                     'uv-manifest', 'uv-verification-request'):
            path = root / (name + '.json')
            if path.is_file():
                out['artifacts'][name] = descriptor(path)
        if command == 'uv.grid-transfer' and grid_accepted:
            for name in ('normal.png', 'receipt.json'):
                path = root / 'grid-transfer' / name
                if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                    out['artifacts']['grid-transfer/' + name] = descriptor(path)
        candidate = root / 'uv-candidate.blend'
        if candidate.is_file():
            out['artifacts']['uv-candidate'] = descriptor(candidate)
    return out
