"""Provider and local ledger operations in the public job lifecycle."""
from contextlib import ExitStack
from pathlib import Path

from protocol import Failure, atomic_json, read_json
from filesystem import FileGuard
from modeling_workflow_contract import input_documents, normalize_params


def run(params, job, checkpoint):
    from modeling_workflow_addon import contract
    from modeling_workflow_addon.providers.registry import run_manifest
    from modeling_workflow_addon.providers.common import ProviderError, RateLimitError, ArtifactAccessError
    from modeling_workflow_addon.providers.image_upload import UploadError
    params = normalize_params(params)
    job = Path(job)
    checkpoint()
    if params['manifest']['operation'] == 'import_local':
        raise Failure('INVALID_REQUEST', 'Local modeling import requires the Blender worker')
    report = {'status': 'running', 'operation': params['manifest']['operation'],
              'provider': params['manifest'].get('provider', 'tripo-cn'),
              'implementation_sha256': params['implementation_sha256'],
              'recovery': 'Query the known task_id; inspect the ledger after an uncertain submit; never automatically resubmit'}
    manifest = params['manifest']
    local_append = manifest['operation'] in ('approval_append_prepare', 'approval_append_apply',
                                            'outstanding_increment_prepare', 'outstanding_increment_apply')
    if local_append:
        report['recovery'] = 'Inspect this job receipts and the current ledger SHA after ambiguity; never restore a financial backup automatically'
    if 'task_id' in manifest:
        report['task_id'] = manifest['task_id']
    if manifest['operation'] in ('download', 'resume_download'):
        report['recovery'] = 'Inspect the failed job; a new download/resume_download job queries the same task for its current URL; no automatic retry'
        report['download_recovery'] = {'operation': manifest['operation'], 'task_id': manifest['task_id']}
        for key in ('recovery_descriptor', 'partial_descriptor'):
            if key in manifest:
                report['download_recovery'][key] = manifest[key]
    atomic_json(job / 'modeling-report.json', report)
    def retain_append_recovery(exc):
        recovery = getattr(exc, 'overseas_recovery', None)
        if manifest.get('provider') == 'tripo-overseas' and isinstance(recovery, dict):
            report['overseas_recovery'] = {key: recovery[key] for key in (
                'accepted_task_id', 'attempt_id', 'acceptance_receipt_file',
                'acceptance_receipt_sha256', 'receipt_persisted',
                'reservation_retained', 'safe_to_resubmit') if key in recovery}
        uploads = getattr(exc, 'uploads', None)
        if isinstance(uploads, list):
            report['uploads'] = list(uploads)
        recovery = getattr(exc, 'pre_submit_recovery', None)
        if manifest['operation'] == 'resume_pre_submit' and isinstance(recovery, dict):
            report['pre_submit_recovery'] = dict(recovery)
        recovery = getattr(exc, 'approval_append_recovery', None)
        if local_append and isinstance(recovery, dict):
            report['approval_recovery'] = dict(recovery)
            for key in ('ledger_file', 'before_sha256', 'expected_after_sha256', 'backup_descriptor',
                        'prepared_receipt_file', 'credits_before', 'attempts_before',
                        'network_calls', 'new_financial_attempts', 'mutation_outcome'):
                if key in recovery:
                    report[key] = recovery[key]
    def batch_progress():
        if params['manifest']['operation'] == 'query_batch' and (job / 'query-batch.json').is_file():
            from modeling_workflow_addon.providers.query_batch import summary
            report['batch_summary'] = summary(read_json(job / 'query-batch.json'))
        if manifest['operation'] in ('submit_batch', 'download_batch') and (job / 'modeling-batch.json').is_file():
            from modeling_workflow_addon.providers.batch import summary
            report['batch_summary'] = summary(read_json(job / 'modeling-batch.json'))
    try:
        with ExitStack() as guards:
            hashes = {}
            for row in input_documents(params):
                guard = guards.enter_context(FileGuard(row['file']))
                hashes[row['file']] = guard.sha256(checkpoint)
                if hashes[row['file']] != row['expected_sha256']:
                    raise Failure('CONFLICT', 'Modeling immutable input changed: ' + row['file'])
            checkpoint()
            if manifest.get('provider') == 'tripo-overseas' or manifest['operation'] in ('submit', 'download', 'resume_download', 'query_batch', 'submit_batch', 'download_batch',
                                         'approval_append_prepare', 'approval_append_apply', 'resume_pre_submit',
                                         'outstanding_increment_prepare', 'outstanding_increment_apply'):
                result = run_manifest(params['manifest'], str(job), checkpoint=checkpoint)
            else:
                result = run_manifest(params['manifest'], str(job))
            if local_append:
                # Retain mutation/receipt identity even if cancellation arrives
                # immediately after an applied append. Overall success still
                # requires the checkpoint and immutable-source postchecks.
                result = dict(result)
                if 'status' in result:
                    report['approval_status'] = result.pop('status')
                report.update(result)
            if manifest['operation'] == 'submit':
                # Acceptance is already durable in the original ledger. Keep
                # its identity and completed uploads before host cancellation.
                for key in ('attempt_id', 'task_id', 'state', 'request_sha256', 'reserved_credits', 'uploads',
                            'task_identity', 'acceptance_receipt_file', 'acceptance_receipt_sha256'):
                    if key in result:
                        report[key] = result[key]
                atomic_json(job / 'modeling-report.json', report)
            if manifest['operation'] == 'resume_pre_submit':
                # Preserve claim/confirmation identity even if the next host
                # checkpoint cancels or immutable-source postchecks fail.
                report['pre_submit_recovery'] = dict(result['pre_submit_recovery'])
                for key in ('attempt_id', 'task_id', 'state'):
                    if key in result:
                        report[key] = result[key]
            checkpoint()
            for filename, sha in hashes.items():
                from protocol import digest
                if digest(filename, checkpoint) != sha:
                    raise Failure('CONFLICT', 'Modeling immutable input changed during host operation')
            result = dict(result)
            if 'status' in result:
                report['remote_status'] = result.pop('status')
            report.update(result)
            report.update(status='passed', implementation_sha256=params['implementation_sha256'], input_hashes=hashes)
            atomic_json(job / 'modeling-report.json', report)
            return report
    except contract.ContractError as exc:
        retain_append_recovery(exc)
        report.update(status='failed', error={'code': 'INVALID_REQUEST', 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    except ProviderError as exc:
        retain_append_recovery(exc)
        batch_progress()
        code = 'RATE_LIMITED' if isinstance(exc, RateLimitError) else 'VALIDATION_FAILED'
        if isinstance(exc, RateLimitError):
            report['rate_limit'] = exc.details
        if isinstance(exc, ArtifactAccessError):
            code = 'ARTIFACT_UNAVAILABLE'
            report['artifact_access'] = exc.details
        if isinstance(exc, UploadError):
            report['upload_failure'] = dict(exc.details)
        report.update(status='failed', error={'code': code, 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise Failure(code, str(exc)) from exc
    except KeyboardInterrupt as exc:
        retain_append_recovery(exc)
        batch_progress()
        message = 'Modeling job interrupted; completed query observations retained'
        report.update(status='failed', error={'code': 'CANCELLED', 'message': message})
        atomic_json(job / 'modeling-report.json', report)
        raise Failure('CANCELLED', message) from None
    except Exception as exc:
        retain_append_recovery(exc)
        batch_progress()
        report.update(status='failed', error={'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise
