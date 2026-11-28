"""Small modeling discovery response and evidence-only compact view."""
from pathlib import Path
from protocol import Failure, digest, read_json, VERSION


def descriptor(path):
    return {'file': str(path), 'sha256': digest(path)}


def describe(operation=None, schema=False, section=None, provider=None, provider_operation=None):
    from modeling_workflow_contract import schemas
    from modeling_identity import implementation_sha256
    from modeling_workflow_addon.providers.registry import catalog
    from modeling_workflow_addon.providers.common import ProviderError
    if provider is not None or provider_operation is not None:
        if operation is not None or section is not None:
            raise Failure('INVALID_REQUEST', 'Provider discovery cannot be combined with manifest operation/section')
        try:
            value = catalog(provider, provider_operation)
        except ProviderError as exc:
            raise Failure('UNSUPPORTED', str(exc)) from None
        if schema:
            if provider_operation is None:
                raise Failure('INVALID_REQUEST','Provider schema requires a provider operation')
            row = value['operation']
            return {'provider':provider,'provider_operation':provider_operation,
                    'schema':row.get('request_schema',row.get('manifest_schema')),
                    'adapter_state':row['adapter_state']}
        return value
    available = schemas()
    if schema:
        if operation not in available:
            raise Failure('INVALID_REQUEST', 'Choose an available modeling schema operation')
        return {'operation': operation, 'schema': available[operation]}
    if section:
        props = available['import_local'].get('properties', {})
        if section not in props:
            raise Failure('INVALID_REQUEST', 'Unknown import_local manifest section')
        return {'section': section, 'schema': props[section]}
    return {'cli_version': VERSION, 'modeling_schema_version': '1.0', 'modeling_schema_versions':['1.0','1.1'],
            'provider_catalog':catalog(), 'operations': ['modeling.run'],
            'manifest_operations': ['import_local', 'submit', 'submit_batch', 'query', 'query_batch', 'download', 'download_batch', 'resume_download', 'bind_unknown', 'account_balance',
                                    'approval_append_prepare', 'approval_append_apply', 'resume_pre_submit',
                                    'outstanding_increment_prepare', 'outstanding_increment_apply'],
            'schema_operations': list(available), 'provider': 'tripo-cn',
            'endpoint': 'https://openapi.tripo3d.com/v3', 'implementation_sha256': implementation_sha256(),
            'state_source': 'SAVED_DISK_V1; no live GUI scene replacement',
            'recovery': 'Query the existing task_id; resume_download requires SHA-pinned native checkpoint and partial descriptors; reviewed bind_unknown maps an unknown reservation without another POST or accounting; uncertain submit is never automatically resubmitted',
            'validation': 'Local GLB uses the native importer and independent reopen. A completed Studio FBX export can use modeling run with studio_export (see --operation studio_fbx --schema). The static adapter verifies local provenance and corner normals on reopen. Overseas basic adapter: mocked requests and read-only balance.',
            'limits': ['Cloud credentials, approved budgets and provider availability are required',
                       'tripo-overseas uses independent scoped credentials/approval/ledger; submit, query, fresh download and account_balance only; no CN recovery or research-ledger migration',
                       'Local import preserves importer output; no retopology or deformation quality claim',
                       'query_batch observes 1-16 known tasks once, sequentially; first query error stops, no polling or retries',
                       'HTTP 429 exposes bounded delay hints only; paid POST outcome remains unknown with reservation retained',
                       'Artifact access failures expose status only; a new same-task download job re-queries the current URL; no refresh guarantee',
                       'submit_batch requires 1-8 approved requests, explicit task GET count/interval; original accepted tasks reused, no POST retries',
                       'download_batch requires 1-16 known tasks with per-item and total byte bounds; completed outputs and failure recovery evidence retained',
                       'approval_append_prepare/apply are local manifest1.1 Tripo operations: exact approval prefix, original ledger path and caps preserved, no credentials or paid task',
                       'Approval append: ordinary ledger1.0 supports dynamic generation/postprocessing parents; legacy ledger1.1 remains independent-generation-only',
                       'outstanding_increment_prepare/apply: local explicit 20-credit/one-attempt contract; freezes ordinary ledger1.0 history including one unknown, authorizes exactly one independent generation, no automatic repeat or settlement',
                       'resume_pre_submit requires fresh caller-reviewed recovery approval, stopped owner and proof generation POST never started; same reserved attempt claimed once, no automatic retry or release; Q43/0.1.12 has one verified real same-reservation recovery case, not a general cloud guarantee']}


def compact(result, requested_command=None):
    out = {k: result[k] for k in ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error') if k in result}
    raw = result.get('artifacts') or {}
    root = Path(raw['job_directory']) if raw.get('job_directory') else None
    report = read_json(root / 'modeling-report.json') if root and (root / 'modeling-report.json').is_file() else result.get('data') or {}
    out['data'] = {k: report[k] for k in ('status', 'operation', 'provider', 'remote_operation', 'task_identity', 'native_status', 'progress', 'available_output_formats', 'accounting', 'candidate', 'candidate_sha256', 'input', 'source_unchanged',
                     'implementation_sha256', 'save_reopen', 'fresh_process_reopen', 'verification', 'input_adapter',
                     'studio_fbx', 'native_normals',
                     'task_id', 'attempt_id', 'binding_sha256', 'reserved_credits', 'accounting_deferred', 'followup_query_required', 'remote_status', 'credits_consumed', 'state', 'recovered_external_task', 'path', 'bytes', 'sha256',
                     'resumed_from', 'resumed_bytes', 'checkpoint_file',
                     'download', 'recovery', 'previews', 'limitations', 'limits', 'error', 'rate_limit', 'batch_summary', 'artifact_access', 'download_recovery', 'uploads', 'upload_failure',
                     'approval_status', 'plan_descriptor', 'ledger_source_descriptor', 'ledger_file', 'request_sha256', 'parent_task',
                     'before_sha256', 'after_sha256', 'expected_after_sha256', 'backup_descriptor', 'receipt_file', 'prepared_receipt_file',
                     'credits_before', 'credits_after', 'attempts_before', 'attempts_after', 'remaining_credits', 'remaining_attempts',
                     'network_calls', 'new_financial_attempts', 'approval_recovery', 'mutation_outcome', 'pre_submit_recovery',
                     'authorization_descriptor', 'frozen_attempt_ids', 'increment_credits', 'increment_attempts',
                     'region', 'base_url', 'balance', 'frozen', 'overseas_recovery',
                     'acceptance_receipt_file', 'acceptance_receipt_sha256') if k in report}
    out['output_format'] = 'compact'
    out['artifacts'] = {'job_directory': str(root)} if root else {}
    if root:
        for name in ('request', 'result', 'modeling-report', 'modeling-reopen', 'query-batch', 'modeling-batch', 'approval-append-plan', 'outstanding-increment-plan', 'modeling-overseas-acceptance'):
            path = root / (name + '.json')
            if path.is_file():
                label = {'query-batch': 'query_batch', 'modeling-batch': 'batch_report'}.get(name, name)
                out['artifacts'][label] = descriptor(path)
        for name, filename in (('recovery_partial', 'original-model.glb.partial'),
                               ('recovery_checkpoint', 'original-model.glb.partial.json')):
            path = root / filename
            if path.is_file():
                out['artifacts'][name] = descriptor(path)
        batch = report.get('batch_summary') or {}
        active = batch.get('active_directory') if batch.get('operation') == 'download_batch' else None
        if isinstance(active, str):
            directory = Path(active).resolve()
            if directory.is_relative_to(root.resolve() / 'batch-items'):
                for name, filename in (('batch_recovery_partial', 'original-model.glb.partial'),
                                       ('batch_recovery_checkpoint', 'original-model.glb.partial.json'),
                                       ('batch_active_output', 'original-model.glb')):
                    path = directory / filename
                    if path.is_file():
                        out['artifacts'][name] = descriptor(path)
    return out
